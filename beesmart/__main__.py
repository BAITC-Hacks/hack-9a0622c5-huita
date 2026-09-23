import uvicorn

from beesmart.config import Settings


if __name__ == "__main__":
    settings = Settings.from_env()
    uvicorn.run("beesmart.api.app:app", host=settings.host, port=settings.port,
                workers=1, access_log=False, log_level=settings.log_level,
                proxy_headers=True, forwarded_allow_ips=settings.forwarded_allow_ips)
