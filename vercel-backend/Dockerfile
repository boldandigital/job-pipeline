# CaptainApply — production webapp image (Phase 1.6)
#
# Same shape as Dockerfile.selfhost, but:
#   - uses vercel-backend/requirements.txt (runtime-only deps, no scrapers/PDF)
#   - exposes Stripe env vars (read by the webapp at request time)
#   - keeps EXPOSE 8742 so the same docker-compose port mapping works
#
# Build & run:
#   docker build -f vercel-backend/Dockerfile -t captainapply/api:latest .
#   docker run -p 8742:8742 \
#     -e CAPTAIN_SESSION_SECRET="$(openssl rand -hex 32)" \
#     -e STRIPE_SECRET_KEY=sk_live_... \
#     -e STRIPE_WEBHOOK_SECRET=whsec_... \
#     -e STRIPE_PRICE_ID=price_... \
#     -e LARS_USER=admin -e LARS_PASS=... \
#     -v captainapply-data:/app/data \
#     captainapply/api:latest

FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    curl \
    tini \
    && rm -rf /var/lib/apt/lists/*

# Runtime deps — no pytest, no httpx, no patchright, no playwright, no weasyprint.
# Matches vercel-backend/requirements.txt.
COPY vercel-backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r /app/requirements.txt \
    && rm -f /app/requirements.txt

# Application code — only the webapp layer, no scrapers, no batch logic.
COPY src/ /app/src/
COPY web/ /app/web/
COPY config/ /app/config/
COPY bin/ /app/bin/

# Writable data directory for SQLite + PDF batches
RUN mkdir -p /app/data
ENV DB_PATH=/app/data/jobs.db
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
ENV CAPTAIN_PROD=1
ENV PORT=8742

# Stripe — required for /api/v1/billing/* (created in Phase 2, but the
# webapp already reads them via os.environ). Surfaced here so Fly.io /
# Render / Railway users don't have to dig for the env var names.
#   STRIPE_SECRET_KEY      sk_live_...  (server)
#   STRIPE_WEBHOOK_SECRET  whsec_...    (for /api/v1/billing/webhook)
#   STRIPE_PRICE_ID        price_...    (the €19/mo plan)
#   STRIPE_PUBLISHABLE_KEY pk_live_...  (sent to the browser, optional)
#
# Set at runtime via `docker run -e STRIPE_SECRET_KEY=sk_live_...` or
# `fly secrets set STRIPE_SECRET_KEY=sk_live_...`. Empty defaults are
# intentional — the webapp treats empty Stripe keys as "billing disabled".

EXPOSE 8742

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://localhost:8742/api/health || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["bash", "-c", "exec python -m uvicorn src.web.app:app --host 0.0.0.0 --port 8742 --workers 1"]
