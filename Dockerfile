FROM denoland/deno:bin AS deno

FROM python:3.12-slim
# yt-dlp needs a JS runtime to solve YouTube's signature/n-challenge; Deno is its default.
COPY --from=deno /deno /usr/local/bin/deno
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ app/
COPY static/ static/
ENV PYTHONUNBUFFERED=1 HOME=/tmp XDG_CACHE_HOME=/tmp/cache DENO_DIR=/tmp/deno CONFIG_DIR=/app/config
# config/ holds the host key, setup state and cookies; it must be writable by the app user.
RUN mkdir -p /app/config && chown 1000:1000 /app/config
USER 1000:1000
EXPOSE 8765
# Only trust X-Forwarded-For from the reverse proxy's network (rate limits and lockouts are per client IP).
ENV TRUSTED_PROXY_CIDR=172.16.0.0/12
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port 8765 --proxy-headers --forwarded-allow-ips \"$TRUSTED_PROXY_CIDR\""]
