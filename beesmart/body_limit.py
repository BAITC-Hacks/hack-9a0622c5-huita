import asyncio

from starlette.responses import JSONResponse


class BodyLimitMiddleware:
    """Bound actual bytes, including requests sent without Content-Length."""

    def __init__(self, app, max_bytes: int = 4096):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] in ("GET", "HEAD", "OPTIONS"):
            await self.app(scope, receive, send)
            return
        if scope.get("path") == "/api/uploads/run" and scope["method"] == "POST":
            # The upload handler counts streamed bytes before the multipart parser.
            await self.app(scope, receive, send)
            return
        body = bytearray()
        try:
            async with asyncio.timeout(5):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    body.extend(message.get("body", b""))
                    if len(body) > self.max_bytes:
                        await JSONResponse({"detail": "Слишком большой запрос"}, status_code=413)(scope, receive, send)
                        return
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            await JSONResponse({"detail": "Время передачи запроса истекло"}, status_code=408)(scope, receive, send)
            return
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)
