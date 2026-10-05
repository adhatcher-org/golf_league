"""HTTP-only administration of generated season weeks."""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from golf_league.database import get_session
from golf_league.security import generate_csrf_token, require_admin, validate_csrf
from golf_league.services.schedule import (
    ScheduleConflict,
    ScheduleValidationError,
    WeekProposal,
    build_default_proposal,
    commit_generated_weeks,
    delete_week,
    edit_week,
    list_weeks,
    schedule_fingerprint,
    week_fingerprint,
)
from golf_league.services.seasons import get_season

router = APIRouter()


def _csrf(request: Request, token: str) -> None:
    if not validate_csrf(request.cookies.get("session") or "", token):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


def _render(request: Request, season, weeks, *, errors=None, proposal=None, preview=False, status_code=200) -> Response:
    token = generate_csrf_token(request.cookies.get("session") or "")
    return request.app.state.templates.TemplateResponse(
        request,
        "admin/weeks/list.html",
        {
            "season": season,
            "weeks": weeks,
            "errors": errors or {},
            "proposal": proposal,
            "preview": preview,
            "csrf_token": token,
            "season_fingerprint": schedule_fingerprint(season),
        },
        status_code=status_code,
    )


@router.get("/admin/seasons/{season_id}/weeks")
async def weeks_page(
    season_id: int,
    request: Request,
    session: Session = Depends(get_session),  # noqa: B008
    admin=Depends(require_admin),  # noqa: B008
) -> Response:
    season = get_season(session, season_id)
    if season is None:
        raise HTTPException(status_code=404, detail="Not found")
    return _render(request, season, list_weeks(session, season_id))


@router.post("/admin/seasons/{season_id}/weeks/generate")
async def generate_weeks_submit(  # noqa: C901
    season_id: int,
    request: Request,
    session: Session = Depends(get_session),  # noqa: B008
    admin=Depends(require_admin),  # noqa: B008
) -> Response:
    form = await request.form()
    _csrf(request, str(form.get("csrf_token", "")))
    season = get_season(session, season_id)
    if season is None:
        raise HTTPException(status_code=404, detail="Not found")
    action = str(form.get("action", ""))
    if action not in {"preview", "commit"}:
        raise HTTPException(status_code=422, detail="Choose preview or commit.")
    if list_weeks(session, season_id):
        raise HTTPException(status_code=409, detail="This season already has weeks; schedule generation is blocked.")
    try:
        rows = build_default_proposal(season, form.get("count", ""))
        proposal = []
        for index, row in enumerate(rows, 1):
            week_type = str(form.get(f"week_type_{index}", row.week_type))
            nine = None if week_type == "rain_date" else row.nine
            status_value = str(form.get(f"status_{index}", row.status))
            makeup_text = str(form.get(f"makeup_{index}", "")).strip()
            if makeup_text and not makeup_text.isdecimal():
                raise ScheduleValidationError({f"makeup_{index}": "Choose a valid makeup target."})
            proposal.append(WeekProposal(
                play_date=row.play_date,
                nine=nine,
                week_type=week_type,
                status=status_value,
                makeup_for_index=int(makeup_text) if makeup_text else None,
                notes=str(form.get(f"notes_{index}", "")).strip() or None,
            ))
        from golf_league.services.schedule import validate_proposal
        validate_proposal(proposal, len(rows))
        if action == "preview":
            return _render(request, season, [], errors={}, proposal=proposal, preview=True)
        if str(form.get("season_fingerprint", "")) != schedule_fingerprint(season):
            raise ScheduleConflict("The season changed after this preview. Review a fresh preview.")
        commit_generated_weeks(
            session,
            season_id,
            proposal=proposal,
            expected_fingerprint=str(form.get("season_fingerprint", "")),
        )
    except ScheduleValidationError as exc:
        try:
            defaults = build_default_proposal(season, form.get("count", ""))
        except ScheduleValidationError:
            defaults = []
        return _render(request, season, [], errors=exc.errors, proposal=defaults, preview=True, status_code=422)
    except ScheduleConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse(f"/admin/seasons/{season_id}/weeks", status_code=303)


@router.get("/admin/seasons/{season_id}/weeks/{week_id}/edit")
async def edit_week_form(
    season_id: int,
    week_id: int,
    request: Request,
    session: Session = Depends(get_session),  # noqa: B008
    admin=Depends(require_admin),  # noqa: B008
) -> Response:
    season = get_season(session, season_id)
    from golf_league.models import Week
    week = session.get(Week, week_id)
    if season is None or week is None or week.season_id != season_id:
        raise HTTPException(status_code=404, detail="Not found")
    return request.app.state.templates.TemplateResponse(
        request,
        "admin/weeks/edit.html",
        {
            "season": season,
            "week": week,
            "weeks": list_weeks(session, season_id),
            "csrf_token": generate_csrf_token(request.cookies.get("session") or ""),
            "fingerprint": week_fingerprint(week),
            "errors": {},
        },
    )


@router.post("/admin/seasons/{season_id}/weeks/{week_id}/edit")
async def edit_week_submit(
    season_id: int,
    week_id: int,
    request: Request,
    session: Session = Depends(get_session),  # noqa: B008
    admin=Depends(require_admin),  # noqa: B008
) -> Response:
    form = await request.form()
    _csrf(request, str(form.get("csrf_token", "")))
    season = get_season(session, season_id)
    from golf_league.models import Week
    week = session.get(Week, week_id)
    if season is None or week is None or week.season_id != season_id:
        raise HTTPException(status_code=404, detail="Not found")
    try:
        edit_week(
            session,
            season_id,
            week_id,
            week_type=str(form.get("week_type", "")),
            status=str(form.get("status", "")),
            makeup_for_index=form.get("makeup_for_index", ""),
            notes=str(form.get("notes", "")),
            expected_fingerprint=str(form.get("fingerprint", "")),
        )
    except ScheduleValidationError as exc:
        return request.app.state.templates.TemplateResponse(
            request,
            "admin/weeks/edit.html",
            {"season": season, "week": week, "weeks": list_weeks(session, season_id),
             "csrf_token": generate_csrf_token(request.cookies.get("session") or ""),
             "fingerprint": week_fingerprint(week), "errors": exc.errors},
            status_code=422,
        )
    except ScheduleConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse(f"/admin/seasons/{season_id}/weeks", status_code=303)


@router.post("/admin/seasons/{season_id}/weeks/{week_id}/delete")
async def delete_week_submit(
    season_id: int,
    week_id: int,
    request: Request,
    session: Session = Depends(get_session),  # noqa: B008
    admin=Depends(require_admin),  # noqa: B008
) -> Response:
    form = await request.form()
    _csrf(request, str(form.get("csrf_token", "")))
    try:
        delete_week(session, season_id, week_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Not found") from exc
    except ScheduleConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse(f"/admin/seasons/{season_id}/weeks", status_code=303)


__all__ = ["router"]
