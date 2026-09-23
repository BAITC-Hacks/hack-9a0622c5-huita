FROM python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    BEESMART_HOST=0.0.0.0 \
    BEESMART_PORT=8000 \
    BEESMART_STORAGE_DIR=/var/lib/beesmart \
    BEESMART_DATA_DIR=/var/lib/beesmart/source

WORKDIR /app
COPY requirements.txt requirements.lock ./
RUN python -m pip install --no-cache-dir -r requirements.txt -c requirements.lock \
    && groupadd --gid 10001 beesmart \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin beesmart \
    && mkdir -p /var/lib/beesmart/source \
    && chown -R 10001:10001 /var/lib/beesmart

# Explicit allowlist: no COPY ., no local CSVs, .env, notebooks, or work artifacts.
COPY agent.py open.json ./
COPY beesmart/ ./beesmart/
COPY organizer/ ./organizer/
COPY policies/ ./policies/
COPY static/ ./static/

USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "beesmart"]
