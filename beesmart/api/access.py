"""Single-team API access; provider credentials never participate in browser auth."""
import ipaddress
import secrets
import time
from collections import OrderedDict

from fastapi import HTTPException, Request
from fastapi.security import HTTPBearer

from beesmart.config import Settings


bearer = HTTPBearer(auto_error=False, scheme_name="BeeSmartToken",
                    description="Токен доступа к BeeSmart из BEESMART_API_TOKEN. Обязателен в production; это не ключ OpenAI.")


class AccessPolicy:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.failures: OrderedDict[str, tuple[int, float]] = OrderedDict()

    def is_authorized(self, authorization: str) -> bool:
        scheme, _, token = authorization.partition(" ")
        return scheme.lower() == "bearer" and secrets.compare_digest(
            token.encode("utf-8"), self.settings.api_token.encode("utf-8"),
        )

    def check(self, request: Request) -> None:
        if self.settings.environment == "local":
            try:
                if request.client and ipaddress.ip_address(request.client.host).is_loopback:
                    return
            except ValueError:
                pass
            raise HTTPException(403, "Локальный режим доступен только на этом компьютере")
        # Schema, code assets and liveness are public; user data never is.
        if not request.url.path.startswith("/api/") or request.url.path == "/api/health":
            return
        if request.url.scheme != "https":
            raise HTTPException(400, "Production API требует HTTPS")
        ip = request.client.host if request.client else "unknown"
        now = time.monotonic()
        count, started = self.failures.get(ip, (0, now))
        if now - started >= 60:
            count, started = 0, now
        if count >= 20:
            raise HTTPException(429, "Слишком много ошибок авторизации", headers={"Retry-After": "60"})
        if not self.is_authorized(request.headers.get("authorization", "")):
            self.failures[ip] = (count + 1, started)
            self.failures.move_to_end(ip)
            while len(self.failures) > 4096:
                self.failures.popitem(last=False)
            raise HTTPException(401, "Нужен токен доступа к BeeSmart", headers={"WWW-Authenticate": "Bearer"})
        self.failures.pop(ip, None)
