from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import text

from golf_league.config import get_settings
from golf_league.database import make_engine
from golf_league.migrations import upgrade_to_head


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    database_url = settings.database_url

    # Create engine
    engine = make_engine(database_url)
    app.state.engine = engine

    # Run migrations to head
    upgrade_to_head(database_url)

    # Mark app as ready after successful migration
    app.state.ready = True

    yield

    # Cleanup on shutdown
    engine.dispose()


def create_app(settings=None) -> FastAPI:
    app = FastAPI(lifespan=lifespan)

    # Health endpoints
    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @app.get("/readyz")
    async def readyz(request: Request) -> Response:
        if not getattr(request.app.state, "ready", False):
            return Response(status_code=503)

        try:
            engine = request.app.state.engine
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return JSONResponse({"status": "ok"})
        except Exception:
            return Response(status_code=503)

    return app


app = create_app()
