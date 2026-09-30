"""HTTP-only admin routes for structural season configuration."""

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from golf_league.config import get_settings
from golf_league.database import get_session
from golf_league.security import generate_csrf_token, require_admin, validate_csrf
from golf_league.services.seasons import (
    SeasonValidationError,
    create_season,
    get_season,
    list_courses_for_season_form,
    season_display_name,
    season_week_count,
    update_season,
)

router = APIRouter()

SESSION_COOKIE_NAME = "session"


def _templates(request: Request):
    return request.app.state.templates


def _require_csrf(request: Request, submitted: str) -> None:
    seed = request.cookies.get(SESSION_COOKIE_NAME) or ""
    if not validate_csrf(seed, submitted):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token"
        )


def _form_values(season=None, submitted: dict[str, str] | None = None) -> dict[str, object]:
    if submitted is not None:
        return submitted
    if season is None:
        return {"year": "", "name_override": "", "course_id": "", "start_date": "", "end_date": "", "play_weekday": "3", "status": "draft", "first_week_nine": "front"}
    return {
        "year": season.year,
        "name_override": season.name_override or "",
        "course_id": season.course_id,
        "start_date": season.start_date.isoformat(),
        "end_date": season.end_date.isoformat(),
        "play_weekday": season.play_weekday,
        "status": season.status,
        "first_week_nine": season.first_week_nine,
    }


def _season_form_context(
    request: Request,
    session: Session,
    *,
    season=None,
    errors: dict[str, str] | None = None,
    submitted: dict[str, str] | None = None,
) -> dict[str, object]:
    seed = request.cookies.get(SESSION_COOKIE_NAME) or ""
    settings = getattr(request.app.state, "settings", None) or get_settings()
    return {
        "season": season,
        "courses": list_courses_for_season_form(session),
        "errors": errors,
        "values": _form_values(season, submitted),
        "csrf_token": generate_csrf_token(seed) if seed else "",
        "display_name": season_display_name(season, settings) if season else None,
        "week_count": season_week_count(season) if season else None,
    }


def _submitted_values(
    *,
    year: str,
    name_override: str,
    course_id: str,
    start_date: str,
    end_date: str,
    play_weekday: str,
    status_value: str,
    first_week_nine: str,
) -> dict[str, str]:
    return {
        "year": year,
        "name_override": name_override,
        "course_id": course_id,
        "start_date": start_date,
        "end_date": end_date,
        "play_weekday": play_weekday,
        "status": status_value,
        "first_week_nine": first_week_nine,
    }


@router.get("/admin/seasons/new")
async def new_season_form(
    request: Request,
    session: Session = Depends(get_session),  # noqa: B008
    admin=Depends(require_admin),  # noqa: B008
) -> Response:
    return _templates(request).TemplateResponse(
        request, "admin/seasons/form.html", _season_form_context(request, session)
    )


@router.post("/admin/seasons/new")
async def create_season_submit(
    request: Request,
    year: str = Form(""),
    name_override: str = Form(""),
    course_id: str = Form(""),
    start_date: str = Form(""),
    end_date: str = Form(""),
    play_weekday: str = Form(""),
    status_value: str = Form("", alias="status"),
    first_week_nine: str = Form(""),
    csrf_token: str = Form(""),
    session: Session = Depends(get_session),  # noqa: B008
    admin=Depends(require_admin),  # noqa: B008
) -> Response:
    _require_csrf(request, csrf_token)
    submitted = _submitted_values(
        year=year,
        name_override=name_override,
        course_id=course_id,
        start_date=start_date,
        end_date=end_date,
        play_weekday=play_weekday,
        status_value=status_value,
        first_week_nine=first_week_nine,
    )
    try:
        season = create_season(session, **submitted)
    except SeasonValidationError as exc:
        return _templates(request).TemplateResponse(
            request,
            "admin/seasons/form.html",
            _season_form_context(request, session, errors=exc.errors),
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    return RedirectResponse(
        url=f"/admin/seasons/{season.id}/edit", status_code=status.HTTP_303_SEE_OTHER
    )


@router.get("/admin/seasons/{season_id}/edit")
async def edit_season_form(
    season_id: int,
    request: Request,
    session: Session = Depends(get_session),  # noqa: B008
    admin=Depends(require_admin),  # noqa: B008
) -> Response:
    season = get_season(session, season_id)
    if season is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return _templates(request).TemplateResponse(
        request,
        "admin/seasons/form.html",
        _season_form_context(request, session, season=season),
    )


@router.post("/admin/seasons/{season_id}/edit")
async def update_season_submit(
    season_id: int,
    request: Request,
    year: str = Form(""),
    name_override: str = Form(""),
    course_id: str = Form(""),
    start_date: str = Form(""),
    end_date: str = Form(""),
    play_weekday: str = Form(""),
    status_value: str = Form("", alias="status"),
    first_week_nine: str = Form(""),
    csrf_token: str = Form(""),
    session: Session = Depends(get_session),  # noqa: B008
    admin=Depends(require_admin),  # noqa: B008
) -> Response:
    _require_csrf(request, csrf_token)
    existing = get_season(session, season_id)
    if existing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    submitted = _submitted_values(
        year=year,
        name_override=name_override,
        course_id=course_id,
        start_date=start_date,
        end_date=end_date,
        play_weekday=play_weekday,
        status_value=status_value,
        first_week_nine=first_week_nine,
    )
    try:
        update_season(session, season_id, **submitted)
    except SeasonValidationError as exc:
        return _templates(request).TemplateResponse(
            request,
            "admin/seasons/form.html",
            _season_form_context(request, session, season=existing, errors=exc.errors),
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    return RedirectResponse(
        url=f"/admin/seasons/{season_id}/edit", status_code=status.HTTP_303_SEE_OTHER
    )
