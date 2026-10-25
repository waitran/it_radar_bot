import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from fastapi.responses import JSONResponse
from .config import Settings
from .db import Catalog
from .favorites import Favorites
from .routes import router
from .security import RateLimiter
from .storage import storage_lock


def create_app(settings: Settings | None = None):
    @asynccontextmanager
    async def lifespan(app):
        with storage_lock(app.state.favorites.path):
            yield

    app = FastAPI(lifespan=lifespan, title='IT Radar Mini App API', docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings or Settings.from_env()
    app.state.catalog = Catalog(app.state.settings.db_path)
    app.state.favorites = Favorites(app.state.settings.user_db_path or
        str(Path(app.state.settings.db_path).with_name('miniapp.db')), app.state.settings.db_path)
    app.state.user_limiter = RateLimiter(app.state.settings.user_rate_limit)
    app.state.ingress_limiter = RateLimiter(app.state.settings.global_rate_limit)
    app.include_router(router)

    @app.middleware('http')
    async def private_response(request, call_next):
        # Do not trust forwarded client IPs; this is an aggregate per-instance cap.
        if request.url.path != '/api/health' and not app.state.ingress_limiter.allow('ingress'):
            return JSONResponse(status_code=429, content={'detail': 'Request limit exceeded'}, headers={'Retry-After': '60', 'Cache-Control': 'no-store'})
        try:
            too_large = int(request.headers.get('content-length', '0')) > 16384
        except ValueError:
            too_large = True
        if too_large or len(request.scope.get('query_string', b'')) > 2048 or len(request.url.path) > 2048:
            return JSONResponse(status_code=413, content={'detail': 'Request too large'}, headers={'Cache-Control': 'no-store'})
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.exception_handler(sqlite3.Error)
    async def database_error(request: Request, exc):
        return JSONResponse(status_code=503, content={'detail': 'Storage temporarily unavailable'}, headers={'Retry-After': '2'})

    @app.exception_handler(ResponseValidationError)
    async def invalid_stored_data(request: Request, exc):
        return JSONResponse(status_code=503, content={'detail': 'Storage temporarily unavailable'})

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc):
        # Never echo raw request values (including authentication) in diagnostics.
        return JSONResponse(status_code=422, content={'detail': 'Invalid request parameters'})

    return app
