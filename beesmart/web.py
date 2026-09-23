import asyncio
import ipaddress
import json
from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from beesmart.config import Settings
from beesmart.body_limit import BodyLimitMiddleware
from beesmart.datasets import DatasetRepository
from beesmart.runs import RunBusyError, RunManager


class StartRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seed: int = Field(default=42, ge=0, le=4_294_967_295, strict=True)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    repository = DatasetRepository(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.runs = RunManager(settings)
        yield
        await app.state.runs.close()

    app = FastAPI(title="BeeSmart API", version="1.0.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None)
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)
    app.add_middleware(BodyLimitMiddleware, max_bytes=4096)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"])

    @app.middleware("http")
    async def local_access_and_headers(request: Request, call_next):
        try:
            loopback = request.client is not None and ipaddress.ip_address(request.client.host).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            return JSONResponse({"detail": "Приложение доступно только на этом компьютере"}, status_code=403)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            expected_origin = f"{request.url.scheme}://{request.headers.get('host', '')}"
            origin = request.headers.get("origin")
            if (origin and origin != expected_origin) or request.headers.get("x-beesmart-request") != "1":
                return JSONResponse({"detail": "Запрос должен исходить из приложения"}, status_code=403)
            try:
                length = int(request.headers.get("content-length", "0"))
            except ValueError:
                length = 1_000_000
            if length > 4096:
                return JSONResponse({"detail": "Слишком большой запрос"}, status_code=413)
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/api/") else "no-cache"
        return response

    @app.get("/api/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/overview")
    def overview():
        return repository.overview()

    @app.post("/api/runs", status_code=202)
    async def start_run(body: StartRunRequest, request: Request):
        overview = await asyncio.to_thread(repository.overview)
        if not overview["runtime"]["ready"]:
            raise HTTPException(409, "Для запуска нужны корректные файлы организаторов и агент")
        try:
            return request.app.state.runs.start(body.seed)
        except RunBusyError:
            raise HTTPException(409, "Расчёт уже идёт. Дождитесь его завершения.") from None

    @app.get("/api/runs/latest")
    async def latest(request: Request):
        result = request.app.state.runs.latest()
        if result is None:
            raise HTTPException(404, "Запусков пока нет")
        return result

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: UUID, request: Request):
        result = request.app.state.runs.get(str(run_id))
        if result is None:
            raise HTTPException(404, "Запуск не найден")
        return result

    @app.get("/api/runs/{run_id}/submission.csv")
    async def submission(run_id: UUID, request: Request):
        content = request.app.state.runs.submission(str(run_id))
        if content is None:
            raise HTTPException(404, "Готового плана пока нет")
        return Response(content, media_type="text/csv", headers={
            "Content-Disposition": 'attachment; filename="submission.csv"',
        })

    @app.get("/api/runs/{run_id}/report.json")
    async def report(run_id: UUID, request: Request):
        result = request.app.state.runs.get(str(run_id))
        if result is None:
            raise HTTPException(404, "Запуск не найден")
        return Response(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False),
                        media_type="application/json", headers={
                            "Content-Disposition": 'attachment; filename="beesmart-report.json"',
                        })

    @app.get("/api/data/{file_id}")
    def download_data(file_id: str):
        path = repository.file_path(file_id)
        if path is None:
            raise HTTPException(404, "Файл не найден")
        return FileResponse(path, filename=path.name, media_type="text/csv")

    @app.get("/")
    async def index():
        path = settings.root / "static" / "index.html"
        if path.is_file():
            return FileResponse(path, media_type="text/html")
        return {"service": "BeeSmart backend", "status": "ok", "overview": "/api/overview",
                "schema": "/openapi.json"}

    app.mount("/static", StaticFiles(directory=settings.root / "static"), name="static")
    return app


app = create_app()
