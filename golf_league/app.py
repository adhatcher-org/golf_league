import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.orm import Session

from golf_league.admin_config import bootstrap_admin
from golf_league.config import get_settings
from golf_league.database import make_engine
from golf_league.domain.rate_limit import RateLimiter
from golf_league.migrations import upgrade_to_head
from golf_league.routers.admin_courses import router as admin_courses_router
from golf_league.routers.identity import router as identity_router
from golf_league.services.course_seed import seed_wyandot
from golf_league.services.email import FakeEmailSender

logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parent.parent
_TEMPLATES_DIR = _BASE_DIR / "templates"
_STATIC_DIR = _BASE_DIR / "static"

LOGIN_RATE_LIMIT = 5
LOGIN_RATE_WINDOW_SECONDS = 15 * 60


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Use provided settings or fall back to get_settings()
    settings = getattr(app.state, "settings", None)
    if settings is None:
        settings = get_settings()
    database_url = settings.database_url

    app.state.ready = False
    app.state.migration_error = None
    engine = None

    try:
        # Create engine and run migrations to head. A failure here must not
        # abort ASGI startup: the app has to stay up so /readyz can honestly
        # report 503 instead of the process refusing connections outright.
        engine = make_engine(database_url)
        app.state.engine = engine

        upgrade_to_head(database_url)

        # First-boot admin bootstrap: a no-op unless `users` is empty. Runs
        # after migrations so the tables it needs are guaranteed to exist.
        bootstrap_session = Session(bind=engine)
        try:
            bootstrap_admin(bootstrap_session)
            if settings.seed_course:
                seed_wyandot(bootstrap_session)
        finally:
            bootstrap_session.close()

        app.state.ready = True
    except Exception as exc:
        logger.exception("startup failed: database/migration error")
        app.state.ready = False
        app.state.migration_error = str(exc)

    yield

    # Cleanup on shutdown
    if engine is not None:
        engine.dispose()


def create_app(settings=None) -> FastAPI:
    app = FastAPI(lifespan=lifespan)

    # Store settings on app state if provided
    if settings is not None:
        app.state.settings = settings

    app.state.templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # Login attempts are throttled per normalized email, not per client, so
    # a single limiter instance (with a real wall clock) is shared across
    # requests. Tests replace this with one that uses a fake clock.
    app.state.login_rate_limiter = RateLimiter(
        limit=LOGIN_RATE_LIMIT,
        window_seconds=LOGIN_RATE_WINDOW_SECONDS,
        clock=time.time,
    )

    # No SMTP exists until M7 (see golf_league/services/email.py); this is
    # a safe in-memory placeholder so /reset always has something to call.
    app.state.email_sender = FakeEmailSender()

    app.include_router(identity_router)
    app.include_router(admin_courses_router)

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
