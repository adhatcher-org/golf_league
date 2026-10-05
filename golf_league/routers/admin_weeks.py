"""HTTP-only administration of generated season weeks."""

import hashlib
import hmac
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from golf_league.config import get_settings
from golf_league.database import get_session
from golf_league.models import Week
from golf_league.security import generate_csrf_token, require_admin, validate_csrf
from golf_league.services.schedule import (
    ScheduleConflict,
    ScheduleValidationError,
    WeekProposal,
    build_default_proposal,
    commit_generated_weeks,
    confirm_nine,
    confirm_shift,
    delete_week,
    edit_week,
    list_weeks,
    preview_nine,
    preview_shift,
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


def _change_resources(session, season_id, week_id):
    season = get_season(session, season_id)
    week = session.get(Week, week_id)
    if season is None or week is None or week.season_id != season_id:
        raise HTTPException(status_code=404, detail="Not found")
    return season, week


def _intent_signature(request, season_id, week_id, action, target, rerotate, fingerprint):
    raw = "|".join(map(str, (season_id, week_id, action, target, rerotate, fingerprint,
                            request.cookies.get("session") or "")))
    settings = getattr(request.app.state, "settings", None) or get_settings()
    secret = settings.session_secret
    return hmac.new(secret.encode(), raw.encode(), hashlib.sha256).hexdigest()


def _render_change(request, season, week, action, *, errors=None, preview=None,
                   target="", rerotate=False, status_code=200):
    signature = ""
    if preview is not None:
        signature = _intent_signature(request, season.id, week.id, action, target,
                                      rerotate, preview.fingerprint)
    return request.app.state.templates.TemplateResponse(
        request, "admin/weeks/change.html",
        {"season": season, "week": week, "action": action, "errors": errors or {},
         "preview": preview, "target": target, "rerotate": rerotate, "signature": signature,
         "csrf_token": generate_csrf_token(request.cookies.get("session") or "")},
        status_code=status_code,
    )


@router.get("/admin/seasons/{season_id}/weeks/{week_id}/shift")
@router.get("/admin/seasons/{season_id}/weeks/{week_id}/nine")
async def week_change_form(season_id: int, week_id: int, request: Request,
                           session: Session = Depends(get_session),  # noqa: B008
                           admin=Depends(require_admin)) -> Response:  # noqa: B008
    season, week = _change_resources(session, season_id, week_id)
    return _render_change(request, season, week, request.url.path.rsplit("/", 1)[-1])


async def _change_submit(season_id, week_id, request, session, action, confirm):  # noqa: C901
    form = await request.form()
    _csrf(request, str(form.get("csrf_token", "")))
    season, week = _change_resources(session, season_id, week_id)
    target = str(form.get("new_date" if action == "shift" else "new_nine", ""))
    rerotate = str(form.get("rerotate", "")) == "1"
    fingerprint = str(form.get("fingerprint", ""))
    if confirm:
        signature = _intent_signature(request, season_id, week_id, action, target, rerotate, fingerprint)
        if not hmac.compare_digest(signature, str(form.get("signature", ""))):
            raise HTTPException(status_code=409, detail="The requested change differs from the preview. Review a fresh preview.")
    try:
        if action == "shift":
            try:
                new_date = date.fromisoformat(target)
            except ValueError as exc:
                raise ScheduleValidationError({"new_date": "Choose a valid calendar date."}) from exc
            if confirm:
                confirm_shift(session, season_id, week_id, new_date=new_date, expected_fingerprint=fingerprint)
            else:
                preview = preview_shift(session, season_id, week_id, new_date=new_date)
        else:
            if confirm:
                confirm_nine(session, season_id, week_id, new_nine=target, rerotate=rerotate,
                             expected_fingerprint=fingerprint)
            else:
                preview = preview_nine(session, season_id, week_id, new_nine=target, rerotate=rerotate)
    except ScheduleValidationError as exc:
        return _render_change(request, season, week, action, errors=exc.errors, status_code=422)
    except ScheduleConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Not found") from exc
    if confirm:
        return RedirectResponse(f"/admin/seasons/{season_id}/weeks", status_code=303)
    return _render_change(request, season, week, action, preview=preview, target=target, rerotate=rerotate)


@router.post("/admin/seasons/{season_id}/weeks/{week_id}/shift/preview")
async def shift_preview_submit(season_id: int, week_id: int, request: Request,
                               session: Session = Depends(get_session),  # noqa: B008
                               admin=Depends(require_admin)) -> Response:  # noqa: B008
    return await _change_submit(season_id, week_id, request, session, "shift", False)


@router.post("/admin/seasons/{season_id}/weeks/{week_id}/shift/confirm")
async def shift_confirm_submit(season_id: int, week_id: int, request: Request,
                               session: Session = Depends(get_session),  # noqa: B008
                               admin=Depends(require_admin)) -> Response:  # noqa: B008
    return await _change_submit(season_id, week_id, request, session, "shift", True)


@router.post("/admin/seasons/{season_id}/weeks/{week_id}/nine/preview")
async def nine_preview_submit(season_id: int, week_id: int, request: Request,
                              session: Session = Depends(get_session),  # noqa: B008
                              admin=Depends(require_admin)) -> Response:  # noqa: B008
    return await _change_submit(season_id, week_id, request, session, "nine", False)


@router.post("/admin/seasons/{season_id}/weeks/{week_id}/nine/confirm")
async def nine_confirm_submit(season_id: int, week_id: int, request: Request,
                              session: Session = Depends(get_session),  # noqa: B008
                              admin=Depends(require_admin)) -> Response:  # noqa: B008
    return await _change_submit(season_id, week_id, request, session, "nine", True)
