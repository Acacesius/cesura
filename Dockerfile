# syntax=docker/dockerfile:1
# Multi-arch (linux/amd64, linux/arm64): both base images publish both.
FROM denoland/deno:bin AS deno

FROM python:3.12-slim
# yt-dlp runs YouTube's player JS to unscramble stream URLs; Deno is its default runtime.
COPY --from=deno /deno /usr/local/bin/deno

# ffmpeg: pocket mode's shared stream (decode the current track, encode one AAC stream).
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Non-root. config/ holds the host key, setup state and cookies.txt, so it must
# be writable by this user (compose's `user:` can override the UID/GID).
RUN groupadd --gid 1000 cesura \
 && useradd --uid 1000 --gid 1000 --no-create-home --home-dir /tmp --shell /usr/sbin/nologin cesura \
 && mkdir -p /app/config && chown cesura:cesura /app/config
COPY app/ app/
COPY static/ static/

ENV PYTHONUNBUFFERED=1 HOME=/tmp XDG_CACHE_HOME=/tmp/cache DENO_DIR=/tmp/deno \
    CONFIG_DIR=/app/config TRUSTED_PROXY_CIDR=172.16.0.0/12
USER cesura
EXPOSE 8765
VOLUME ["/app/config"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=4).status == 200 else 1)"]

# X-Forwarded-For is only trusted from TRUSTED_PROXY_CIDR (your reverse proxy's network).
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port 8765 --proxy-headers --forwarded-allow-ips \"$TRUSTED_PROXY_CIDR\""]
