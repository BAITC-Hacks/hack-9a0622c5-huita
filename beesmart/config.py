import ipaddress
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parent.parent
LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]", "testserver")


def _csv(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(part.strip() for part in value.split(",") if part.strip()))


@dataclass(frozen=True, slots=True)
class Settings:
    root: Path = ROOT
    run_timeout_seconds: float = 540
    retained_runs: int = 20
    max_events: int = 400
    environment: str = "local"
    host: str = "127.0.0.1"
    port: int = 8000
    api_token: str = field(default="", repr=False)
    allowed_hosts: tuple[str, ...] = LOCAL_HOSTS
    allowed_origins: tuple[str, ...] = ()
    storage_dir: Path | None = None
    data_dir: Path | None = None
    forwarded_allow_ips: str = "127.0.0.1"
    log_level: str = "info"
    agent_provider: str = "local"
    openai_api_key: str = field(default="", repr=False)
    openai_model: str = "gpt-6-luna"
    llm_budget_usd: float = 5.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root).resolve())
        if self.environment not in ("local", "production"):
            raise ValueError("BEESMART_ENVIRONMENT must be local or production")
        if not self.host or any(character.isspace() or ord(character) < 32 for character in self.host):
            raise ValueError("BEESMART_HOST must be a valid bind address")
        if not isinstance(self.port, int) or not 1 <= self.port <= 65535:
            raise ValueError("BEESMART_PORT must be between 1 and 65535")
        if self.log_level not in ("critical", "error", "warning", "info", "debug"):
            raise ValueError("BEESMART_LOG_LEVEL is invalid")
        if self.agent_provider not in ("local", "openai"):
            raise ValueError("BEESMART_AGENT_PROVIDER must be local or openai")
        if self.openai_model != "gpt-6-luna":
            raise ValueError("BEESMART_OPENAI_MODEL must be gpt-6-luna (the budgeted model)")
        if not math.isfinite(self.llm_budget_usd) or not 0 <= self.llm_budget_usd <= 50:
            raise ValueError("BEESMART_LLM_BUDGET_USD must be between 0 and 50")
        if self.openai_api_key and (not self.openai_api_key.isascii() or
                any(not 33 <= ord(c) <= 126 for c in self.openai_api_key)):
            raise ValueError("OPENAI_API_KEY contains invalid characters")
        if self.run_timeout_seconds <= 0 or self.retained_runs < 1 or self.max_events < 1:
            raise ValueError("Run timeout, retention, and event limits must be positive")
        if not self.allowed_hosts:
            raise ValueError("BEESMART_ALLOWED_HOSTS must list explicit hostnames")
        for hostname in self.allowed_hosts:
            if not hostname or "*" in hostname or any(c.isspace() for c in hostname) or "/" in hostname or "://" in hostname:
                raise ValueError("BEESMART_ALLOWED_HOSTS must contain hostnames without schemes, paths, or wildcards")
        for origin in self.allowed_origins:
            try:
                parsed = urlsplit(origin)
                _ = parsed.port
            except ValueError:
                raise ValueError("BEESMART_ALLOWED_ORIGINS contains an invalid origin") from None
            allowed_schemes = ("https",) if self.environment == "production" else ("http", "https")
            if (parsed.scheme not in allowed_schemes or not parsed.hostname or "*" in origin
                    or parsed.username is not None or parsed.password is not None
                    or parsed.path or parsed.query or parsed.fragment or any(c.isspace() for c in origin)):
                raise ValueError("BEESMART_ALLOWED_ORIGINS must contain exact origins; production requires HTTPS")
        for address in _csv(self.forwarded_allow_ips):
            try:
                ipaddress.ip_address(address)
            except ValueError:
                raise ValueError("BEESMART_FORWARDED_ALLOW_IPS accepts explicit proxy IP addresses only") from None
        if self.environment == "production":
            if len(self.api_token) < 32 or not self.api_token.isascii() or any(not 33 <= ord(c) <= 126 for c in self.api_token):
                raise ValueError("Production requires BEESMART_API_TOKEN with at least 32 non-whitespace ASCII characters")
            if self.allowed_hosts == LOCAL_HOSTS:
                raise ValueError("Production requires an explicit BEESMART_ALLOWED_HOSTS setting")

    @property
    def storage_path(self) -> Path:
        value = Path(self.storage_dir) if self.storage_dir is not None else Path("work")
        return (value if value.is_absolute() else self.root / value).resolve()

    @property
    def data_path(self) -> Path:
        value = Path(self.data_dir) if self.data_dir is not None else self.root
        return (value if value.is_absolute() else self.root / value).resolve()

    @classmethod
    def from_env(cls, root: Path | str | None = None) -> "Settings":
        from dotenv import load_dotenv

        base = Path(root).resolve() if root is not None else ROOT
        # Explicit process/secret-manager values take precedence over local
        # developer configuration. Provider credentials stay in this process.
        load_dotenv(base / ".env", override=False)
        environment = os.environ.get("BEESMART_ENVIRONMENT", "local").strip().lower()
        try:
            port = int(os.environ.get("BEESMART_PORT", "8000"))
        except ValueError:
            raise ValueError("BEESMART_PORT must be an integer") from None
        try:
            llm_budget = float(os.environ.get("BEESMART_LLM_BUDGET_USD", "5"))
        except ValueError:
            raise ValueError("BEESMART_LLM_BUDGET_USD must be a number") from None
        storage, data = os.environ.get("BEESMART_STORAGE_DIR", ""), os.environ.get("BEESMART_DATA_DIR", "")
        return cls(
            root=base,
            environment=environment,
            host=os.environ.get("BEESMART_HOST", "127.0.0.1").strip(),
            port=port,
            api_token=os.environ.get("BEESMART_API_TOKEN", ""),
            allowed_hosts=_csv(os.environ.get("BEESMART_ALLOWED_HOSTS", "" if environment == "production" else ",".join(LOCAL_HOSTS))),
            allowed_origins=_csv(os.environ.get("BEESMART_ALLOWED_ORIGINS", "")),
            storage_dir=Path(storage) if storage else None,
            data_dir=Path(data) if data else None,
            forwarded_allow_ips=os.environ.get("BEESMART_FORWARDED_ALLOW_IPS", "127.0.0.1"),
            log_level=os.environ.get("BEESMART_LOG_LEVEL", "info").lower(),
            agent_provider=os.environ.get("BEESMART_AGENT_PROVIDER", "local").strip().lower(),
            openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
            openai_model=os.environ.get("BEESMART_OPENAI_MODEL", "gpt-6-luna"),
            llm_budget_usd=llm_budget,
        )


LIMITS = {
    "total_budget": 100_000,
    "max_total_contacts": 15_000,
    "max_campaigns": 10,
    "max_pilots": 20,
    "max_customers_per_campaign": 5_000,
}
