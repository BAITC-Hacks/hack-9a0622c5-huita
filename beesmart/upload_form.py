"""Multipart parsing with byte/time limits, including chunked requests."""
import asyncio
from contextlib import asynccontextmanager

from fastapi import HTTPException, Request
from starlette.datastructures import UploadFile


MAX_UPLOAD_BYTES = 50 * 1024 * 1024


@asynccontextmanager
async def upload_form(request: Request):
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "multipart/form-data":
        raise HTTPException(415, "Передайте profile, history и tariffs как multipart/form-data")
    received = 0

    async def limited_receive():
        nonlocal received
        message = await request.receive()
        received += len(message.get("body", b""))
        if received > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "Общий размер загрузки превышает 50 МиБ")
        return message

    bounded = Request(request.scope, receive=limited_receive)
    try:
        # Starlette spools file parts to disk and closes partial files on parser errors.
        async with asyncio.timeout(30):
            form = await bounded.form(max_files=3, max_fields=1, max_part_size=1024)
    except TimeoutError:
        raise HTTPException(408, "Время загрузки файлов истекло") from None
    try:
        roles = {"profile", "history", "tariffs"}
        if set(form) - roles - {"seed"} or any(len(form.getlist(role)) != 1 for role in roles):
            raise HTTPException(422, "Нужны ровно три файла: profile, history, tariffs")
        files = {role: form[role] for role in roles}
        if any(not isinstance(value, UploadFile) for value in files.values()):
            raise HTTPException(422, "profile, history и tariffs должны быть CSV-файлами")
        raw_seed = form.get("seed", "42")
        if not isinstance(raw_seed, str) or not raw_seed.isascii() or not raw_seed.isdecimal():
            raise HTTPException(422, "seed должен быть целым числом от 0 до 4294967295")
        seed = int(raw_seed)
        if seed > 4_294_967_295:
            raise HTTPException(422, "seed должен быть целым числом от 0 до 4294967295")
        yield files, seed
    finally:
        await form.close()
