"""Роздача зібраної панелі з того самого процесу.

Навіщо: у Docker-конфігурації статику віддає nginx, але для локального
запуску («один батник — робочий веб») тримати ще й окремий dev-сервер
незручно. Якщо `frontend/dist` зібраний, бекенд віддає його на `/` — і
панель, і API живуть на одному origin, тож CORS і CSP не заважають.

Якщо збірки немає, `/` повертає коротку довідку з посиланнями замість
голого 404 — саме він раніше виглядав як «порожня сторінка».

Маршрути API (`/api`, `/ws`, `/health`, `/docs`) реєструються **до** цього
модуля й ним не перекриваються: SPA-фолбек навмисно ігнорує їхні префікси.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

logger = logging.getLogger(__name__)

# backend/app/api/spa.py → parents[3] = корінь репозиторію
FRONTEND_DIST = Path(__file__).resolve().parents[3] / "frontend" / "dist"

# Префікси, які належать бекенду: SPA-фолбек їх не чіпає.
API_PREFIXES = ("/api", "/ws", "/health", "/docs", "/redoc", "/openapi.json")


def frontend_available() -> bool:
    return (FRONTEND_DIST / "index.html").is_file()


def mount_frontend(app: FastAPI) -> None:
    """Підключає зібрану панель, якщо вона є."""
    if not frontend_available():
        _mount_placeholder(app)
        return

    # Хешовані ассети віддаємо як статику.
    assets = FRONTEND_DIST / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    index = FRONTEND_DIST / "index.html"

    @app.get("/", include_in_schema=False)
    async def spa_root() -> FileResponse:
        return FileResponse(index)

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(request: Request, full_path: str):
        """Будь-який маршрут панелі віддає index.html (client-side routing)."""
        path = "/" + full_path
        if path.startswith(API_PREFIXES):
            # Не наша справа: хай відпрацює штатний 404 API.
            return JSONResponse(
                {"error": {"code": "not_found", "message": "Not Found", "details": {}}},
                status_code=404,
            )

        # Реальний файл (favicon тощо) віддаємо як є.
        candidate = (FRONTEND_DIST / full_path).resolve()
        if FRONTEND_DIST in candidate.parents and candidate.is_file():
            return FileResponse(candidate)

        return FileResponse(index)

    logger.info("Панель віддається з %s", FRONTEND_DIST)


def _mount_placeholder(app: FastAPI) -> None:
    """Панель не зібрана — пояснюємо, що робити, замість порожньої сторінки."""

    @app.get("/", include_in_schema=False)
    async def root_hint() -> HTMLResponse:
        return HTMLResponse(
            """<!doctype html>
<html lang="uk"><head><meta charset="utf-8"><title>Fleet MDM</title>
<style>
 body{font:16px/1.6 system-ui,sans-serif;max-width:40rem;margin:4rem auto;padding:0 1rem;
      background:#0f1115;color:#e6e8eb}
 code{background:#1b1f27;padding:.15rem .4rem;border-radius:4px}
 a{color:#6ea8fe} .muted{color:#9aa4b2;font-size:.9rem}
</style></head><body>
<h1>Fleet MDM — бекенд працює</h1>
<p>Але зібраної панелі немає, тому показувати нічого.</p>
<h2>Що доступно зараз</h2>
<ul>
  <li><a href="/docs">/docs</a> — інтерактивна документація API</li>
  <li><a href="/health">/health</a> — стан сервісу</li>
</ul>
<h2>Щоб з'явилася панель</h2>
<p>Зберіть її один раз:</p>
<p><code>cd frontend &amp;&amp; npm install &amp;&amp; npm run build</code></p>
<p>і перезапустіть бекенд. Або запустіть <code>start-backend.bat</code> —
він зробить це сам, якщо встановлено Node.js.</p>
<p class="muted">Для розробки з гарячим перезавантаженням:
<code>npm run dev</code> у каталозі <code>frontend</code>
(панель буде на <a href="http://localhost:5173">:5173</a>).</p>
</body></html>"""
        )
