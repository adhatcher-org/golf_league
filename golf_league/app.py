import asyncio
import logging
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
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
from golf_league.logging_config import apply_log_level, install_access_log_filter
from golf_league.migrations import upgrade_to_head
from golf_league.routers.admin_config import router as admin_config_router
from golf_league.routers.admin_courses import router as admin_courses_router
from golf_league.routers.admin_imports import router as admin_imports_router
from golf_league.routers.admin_invites import InviteReceiptStore
from golf_league.routers.admin_invites import router as admin_invites_router
from golf_league.routers.admin_matches import router as admin_matches_router
from golf_league.routers.admin_participants import router as admin_participants_router
from golf_league.routers.admin_roster import router as admin_roster_router
from golf_league.routers.admin_seasons import router as admin_seasons_router
from golf_league.routers.admin_teams import router as admin_teams_router
from golf_league.routers.admin_weeks import router as admin_weeks_router
from golf_league.routers.home import router as home_router
from golf_league.routers.identity import router as identity_router
from golf_league.routers.join import router as join_router
from golf_league.routers.player_schedule import router as player_schedule_router
from golf_league.routers.roster import router as roster_router
from golf_league.security import (
    SESSION_COOKIE_NAME,
    generate_csrf_token,
    get_optional_user,
)
from golf_league.services.course_seed import seed_wyandot
from golf_league.services.email import FakeEmailSender

logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parent.parent
_TEMPLATES_DIR = _BASE_DIR / "templates"
_STATIC_DIR = _BASE_DIR / "static"

LOGIN_RATE_LIMIT = 5
LOGIN_RATE_WINDOW_SECONDS = 15 * 60
REGISTRATION_RATE_LIMIT = 3
REGISTRATION_RATE_WINDOW_SECONDS = 15 * 60


async def _navigation_user(request: Request, call_next):
    """Provide verified session identity to the shared navigation template."""
    request.state.current_user = None
    request.state.can_view_player_pages = False
    request.state.nav_csrf_token = ""

    is_page_request = (
        request.url.path not in {"/healthz", "/readyz"}
        and not request.url.path.startswith("/static/")
    )
    cookie_value = request.cookies.get(SESSION_COOKIE_NAME)
    engine = getattr(request.app.state, "engine", None)
    if is_page_request and cookie_value and engine is not None:
        try:
            with Session(bind=engine) as nav_session:
                user = await get_optional_user(request, nav_session)
            if user is not None:
                current_settings = getattr(request.app.state, "settings", None)
                if current_settings is None:
                    current_settings = get_settings()
                request.state.current_user = user
                request.state.can_view_player_pages = (
                    user.email_verified_at is not None
                    or not current_settings.email_verification_required
                )
                request.state.nav_csrf_token = generate_csrf_token(cookie_value)
        except Exception:
            logger.warning("navigation identity lookup failed")

    return await call_next(request)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Use provided settings or fall back to get_settings()
    settings = getattr(app.state, "settings", None)
    if settings is None:
        settings = get_settings()
    # Settings are resolved once per process: the managed file is read here
    # and every managed setting is restart-required (R-DEPLOYMENT).
    app.state.settings = settings
    apply_log_level(settings.log_level)
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
    install_access_log_filter()
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
    app.state.registration_rate_limiter = RateLimiter(
        limit=REGISTRATION_RATE_LIMIT,
        window_seconds=REGISTRATION_RATE_WINDOW_SECONDS,
        clock=time.time,
    )

    # No SMTP exists until M7 (see golf_league/services/email.py); this is
    # a safe in-memory placeholder so /reset always has something to call.
    app.state.email_sender = FakeEmailSender()
    app.state.invite_receipts = InviteReceiptStore()
    app.state.join_monotonic = time.monotonic
    app.state.join_sleep = asyncio.sleep
    app.state.join_utc_now = lambda: datetime.now(UTC)

    app.middleware("http")(_navigation_user)

    app.include_router(identity_router)
    app.include_router(join_router)
    app.include_router(home_router)
    app.include_router(roster_router)
    app.include_router(player_schedule_router)
    app.include_router(admin_config_router)
    app.include_router(admin_courses_router)
    app.include_router(admin_roster_router)
    app.include_router(admin_imports_router)
    app.include_router(admin_invites_router)
    app.include_router(admin_seasons_router)
    app.include_router(admin_participants_router)
    app.include_router(admin_teams_router)
    app.include_router(admin_weeks_router)
    app.include_router(admin_matches_router)

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
