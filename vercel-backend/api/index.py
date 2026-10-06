"""Vercel Functions entrypoint for the CaptainApply FastAPI backend.

Deploy:
  vercel link --project captainapply-api
  vercel --prod

The repo's existing FastAPI app (src/web/app.py) is the source of truth.
This file is the *thinnest possible* ASGI shim so Vercel's Python runtime
can serve it as a serverless function.

Heads up on the timeout:
  Vercel hobby tier caps serverless functions at 10s. Apply endpoints that
  hit Greenhouse / Lever / Workday can run longer (browser automation,
  multi-step form filling, captchas). For production traffic we recommend
  either:

    (a) Vercel Pro with `maxDuration: 60` set in vercel.json per route, or
    (b) Deploy the same Docker image to Fly.io / Render / Railway
        (see vercel-backend/Dockerfile + docker-compose.yml in this repo).

  The signup / verify / login / dashboard endpoints are all under 200ms
  and work fine on the 10s hobby limit.
"""

from src.web.app import app  # noqa: F401  (re-exported for Vercel)

# Vercel looks for either `app` (ASGI/WSGI callable) or `handler`.
# FastAPI instances are ASGI, so `app` is the handler.
handler = app
