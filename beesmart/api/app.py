import asyncio
import json
import logging
from contextlib import asynccontextmanager, suppress
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, Security
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.middleware.cors import CORSMiddleware

from beesmart.config import Settings
from beesmart.api.body_limit import BodyLimitMiddleware
from beesmart.application.datasets import DatasetRepository
from beesmart.application.runs import RunBusyError, RunManager
from beesmart.agent.llm import LLMError
from beesmart.api.upload_form import MAX_UPLOAD_BYTES, upload_form
from beesmart.application.uploads import UploadStore, UploadValidationError
from beesmart.api.access import AccessPolicy, bearer
from beesmart.application.storage import StorageLease
from beesmart.application.contracts import HealthResponse, RunRecord, OverviewResponse, ErrorResponse, AgentInfo


class StartRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seed: int = Field(default=42, ge=0, le=4_294_967_295, strict=True)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    repository = DatasetRepository(settings)
    policy = AccessPolicy(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        lease = StorageLease(settings.storage_path)
        lease.acquire()
        try:
            app.state.uploads = UploadStore(settings.storage_path / "datasets")
            app.state.runs = RunManager(settings)
            try:
                yield
            finally:
                await app.state.runs.close()
        finally:
            lease.release()

    app = FastAPI(title="BeeSmart API", version="1.3.0", lifespan=lifespan,
                  description="Загрузка CSV → OpenAI выбирает гипотезы по агрегатам → Python проверяет пилоты и план. Поддерживается автономный local режим. В production требуется Bearer-токен BeeSmart; ключ OpenAI хранится только на сервере.",
                  docs_url=None, redoc_url=None)
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)
    app.add_middleware(BodyLimitMiddleware, max_bytes=4096)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))

    async def api_security(request: Request, credentials=Security(bearer)):
        policy.check(request)

    async def mutation_header(x_beesmart_request: str = Header(..., pattern="^1$")):
        """Explicit header in the frontend contract; middleware validates before body parsing."""

    api = APIRouter(dependencies=[Security(api_security)], responses={
        code: {"model": ErrorResponse} for code in (400, 401, 403, 404, 408, 409, 413, 415, 422, 429, 503)
    })

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        return JSONResponse({"detail": "Некорректные параметры запроса"}, status_code=422)

    @app.middleware("http")
    async def access_and_headers(request: Request, call_next):
        try:
            policy.check(request)
        except HTTPException as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code,
                                headers={"Cache-Control": "no-store", **(exc.headers or {})})
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            expected_origin = f"{request.url.scheme}://{request.headers.get('host', '')}"
            origin = request.headers.get("origin")
            if (origin and origin != expected_origin and origin not in settings.allowed_origins) or request.headers.get("x-beesmart-request") != "1":
                return JSONResponse({"detail": "Запрос должен исходить из приложения"}, status_code=403)
            try:
                length = int(request.headers.get("content-length", "0"))
            except ValueError:
                return JSONResponse({"detail": "Некорректный Content-Length"}, status_code=400)
            limit = MAX_UPLOAD_BYTES if request.url.path == "/api/uploads/run" else 4096
            if length < 0 or length > limit:
                return JSONResponse({"detail": "Слишком большой запрос"}, status_code=413)
        try:
            response = await call_next(request)
        except Exception as exc:
            logging.getLogger(__name__).error("Request failed (%s)", type(exc).__name__)
            response = JSONResponse({"detail": "Внутренняя ошибка сервера"}, status_code=500)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        if settings.environment == "production":
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        response.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/api/") else "no-cache"
        return response

    @app.get("/api/health", response_model=HealthResponse, tags=["System"])
    async def health():
        return {"status": "ok"}

    @api.get("/api/overview", response_model=OverviewResponse, tags=["Data"])
    async def overview(request: Request):
        result = await asyncio.to_thread(repository.overview)
        result["agent"] = request.app.state.runs.intelligence.info()
        for provider in result["providers"]:
            if provider["id"] == "openai":
                provider["app_spend_usd"] = result["agent"]["estimated_spend_usd"]
                provider["runtime_calls"] = result["agent"]["paid_calls"]
        return result

    @api.get("/api/agent", response_model=AgentInfo, tags=["System"])
    async def agent_info(request: Request):
        return request.app.state.runs.intelligence.info()

    @api.post("/api/runs", status_code=202, response_model=RunRecord, response_model_exclude_unset=True,
              dependencies=[Depends(mutation_header)], tags=["Runs"])
    async def start_run(body: StartRunRequest, request: Request):
        try:
            request.app.state.runs.intelligence.check_ready()
        except LLMError as exc:
            raise HTTPException(503, str(exc)) from None
        overview = await asyncio.to_thread(repository.overview)
        if not overview["runtime"]["ready"]:
            raise HTTPException(409, "Для запуска нужны корректные файлы организаторов и агент")
        try:
            return await request.app.state.runs.start(body.seed)
        except RunBusyError:
            raise HTTPException(409, "Расчёт уже идёт. Дождитесь его завершения.") from None
        except OSError:
            raise HTTPException(503, "Хранилище запусков недоступно") from None

    @api.post("/api/uploads/run", status_code=202, response_model=RunRecord, response_model_exclude_unset=True,
              dependencies=[Depends(mutation_header)], tags=["Runs"], openapi_extra={
        "requestBody": {"required": True, "content": {"multipart/form-data": {"schema": {
            "type": "object", "required": ["profile", "history", "tariffs"],
            "properties": {
                "profile": {"type": "string", "format": "binary"},
                "history": {"type": "string", "format": "binary"},
                "tariffs": {"type": "string", "format": "binary"},
                "seed": {"type": "integer", "default": 42, "minimum": 0, "maximum": 4_294_967_295},
            },
        }}}},
    })
    async def upload_and_run(request: Request):
        runs = request.app.state.runs
        uploads = request.app.state.uploads
        try:
            runs.reserve_upload()
        except LLMError as exc:
            raise HTTPException(503, str(exc)) from None
        except RunBusyError:
            raise HTTPException(409, "Загрузка или расчёт уже идёт. Дождитесь завершения.") from None
        try:
            if any(not (settings.root / name).is_file() for name in repository.RUNTIME_FILES):
                raise HTTPException(409, "Для запуска нужны файлы среды организаторов и агент")
            async with upload_form(request) as (files, seed):
                saving = asyncio.create_task(asyncio.to_thread(
                    uploads.create, {role: item.file for role, item in files.items()},
                ))
                try:
                    dataset = await asyncio.shield(saving)
                except asyncio.CancelledError:
                    # A second cancellation must not close multipart files or
                    # release the slot while the saving thread still uses them.
                    with suppress(UploadValidationError, OSError):
                        outcome = await runs._finish_persistence(saving)
                        cleanup = asyncio.create_task(asyncio.to_thread(uploads.discard, outcome["id"]))
                        await runs._finish_persistence(cleanup)
                    raise
                return await runs.start(seed, dataset=dataset, from_upload=True)
        except UploadValidationError as exc:
            raise HTTPException(422, str(exc)) from None
        except OSError:
            raise HTTPException(503, "Не удалось сохранить загруженные файлы") from None
        finally:
            runs.release_upload()

    @api.get("/api/runs/latest", response_model=RunRecord, response_model_exclude_unset=True, tags=["Runs"])
    async def latest(request: Request):
        result = request.app.state.runs.latest()
        if result is None:
            raise HTTPException(404, "Запусков пока нет")
        return result

    @api.get("/api/runs/{run_id}", response_model=RunRecord, response_model_exclude_unset=True, tags=["Runs"])
    async def get_run(run_id: UUID, request: Request):
        result = request.app.state.runs.get(str(run_id))
        if result is None:
            raise HTTPException(404, "Запуск не найден")
        return result

    @api.get("/api/runs/{run_id}/submission.csv", tags=["Exports"], response_class=Response,
             responses={200: {"content": {"text/csv": {"schema": {"type": "string"}}}}})
    async def submission(run_id: UUID, request: Request):
        content = request.app.state.runs.submission(str(run_id))
        if content is None:
            raise HTTPException(404, "Готового плана пока нет")
        return Response(content, media_type="text/csv", headers={
            "Content-Disposition": 'attachment; filename="submission.csv"',
        })

    @api.get("/api/runs/{run_id}/report.json", response_model=RunRecord, tags=["Exports"])
    async def report(run_id: UUID, request: Request):
        result = request.app.state.runs.get(str(run_id))
        if result is None:
            raise HTTPException(404, "Запуск не найден")
        return Response(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False),
                        media_type="application/json", headers={
                            "Content-Disposition": 'attachment; filename="beesmart-report.json"',
                        })

    @api.get("/api/data/{file_id}", tags=["Data"], response_class=FileResponse,
             responses={200: {"content": {"text/csv": {"schema": {"type": "string", "format": "binary"}}}}})
    def download_data(file_id: str):
        path = repository.file_path(file_id)
        if path is None:
            raise HTTPException(404, "Файл не найден")
        return FileResponse(path, filename=path.name, media_type="text/csv")

    @app.get("/open.json", include_in_schema=False)
    async def schema_alias():
        return JSONResponse(app.openapi())

    @app.get("/", include_in_schema=False)
    async def index():
        path = settings.root / "static" / "index.html"
        if path.is_file():
            return FileResponse(path, media_type="text/html")
        return {"service": "BeeSmart backend", "status": "ok", "overview": "/api/overview",
                "schema": "/openapi.json"}

    app.include_router(api)
    app.mount("/static", StaticFiles(directory=settings.root / "static", check_dir=False), name="static")
    app.add_middleware(CORSMiddleware, allow_origins=list(settings.allowed_origins),
                       allow_methods=["GET", "POST", "OPTIONS"],
                       allow_headers=["Authorization", "Content-Type", "X-BeeSmart-Request"],
                       expose_headers=["Content-Disposition"], allow_credentials=False)
    return app


app = create_app()
