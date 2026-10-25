import sqlite3
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from fastapi.responses import JSONResponse
from .config import Settings
from .db import Catalog
from .routes import router


def create_app(settings: Settings | None = None):
    app = FastAPI(title='IT Radar Mini App API', docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings or Settings.from_env()
    app.state.catalog = Catalog(app.state.settings.db_path)
    app.include_router(router)

    @app.middleware('http')
    async def private_response(request, call_next):
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.exception_handler(sqlite3.Error)
    async def database_error(request: Request, exc):
        return JSONResponse(status_code=503, content={'detail': 'Catalog temporarily unavailable'}, headers={'Retry-After': '2'})

    @app.exception_handler(ResponseValidationError)
    async def invalid_stored_data(request: Request, exc):
        return JSONResponse(status_code=503, content={'detail': 'Catalog temporarily unavailable'})

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc):
        # Never echo raw request values (including authentication) in diagnostics.
        return JSONResponse(status_code=422, content={'detail': 'Invalid request parameters'})

    return app
