"""Admin routes for the roster.

Routes only: every database access goes through
`golf_league.services.roster` (and, for the tee-set picker,
`golf_league.services.courses`). `golf_league.models` is never imported
here.
"""

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from golf_league.database import get_session
from golf_league.security import generate_csrf_token, require_admin, validate_csrf
from golf_league.services.courses import list_courses_with_tee_sets
from golf_league.services.roster import (
    RosterValidationError,
    create_golfer,
    get_golfer,
    list_golfers,
    update_golfer,
)

router = APIRouter()

SESSION_COOKIE_NAME = "session"


def _templates(request: Request):
    return request.app.state.templates


def _csrf_seed(request: Request) -> str:
    return request.cookies.get(SESSION_COOKIE_NAME) or ""


def _require_csrf(request: Request, submitted: str) -> None:
    seed = _csrf_seed(request)
    if not validate_csrf(seed, submitted):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token"
        )


def _golfer_form_context(
    request: Request,
    session: Session,
    *,
    golfer=None,
    errors=None,
    selected_tee_set_id: int | None = None,
    rendered_tee_set_id: int | str | None = None,
    handicap_display: str | None = None,
) -> dict:
    seed = _csrf_seed(request)
    return {
        "golfer": golfer,
        "courses": list_courses_with_tee_sets(session),
        "errors": errors,
        "csrf_token": generate_csrf_token(seed) if seed else "",
        "selected_tee_set_id": selected_tee_set_id,
        "rendered_tee_set_id": rendered_tee_set_id,
        "handicap_display": handicap_display,
    }


def _try_parse_int(value: object) -> int | None:
    """Return `int(value)`, or None when it does not parse cleanly."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _tee_selection_confirmed(rendered_tee_set_id: str, submitted_tee_set_id: str) -> bool:
    """Return True only when both values parse and name the same tee set.

    A missing, empty or non-numeric `rendered_tee_set_id` always counts as
    differing from the submitted tee, so a hand-built request cannot skip
    the two-step confirmation.
    """
    rendered = _try_parse_int(rendered_tee_set_id)
    submitted = _try_parse_int(submitted_tee_set_id)
    if rendered is None or submitted is None:
        return False
    return rendered == submitted


@router.get("/admin/golfers")
async def list_golfers_route(
    request: Request,
    session: Session = Depends(get_session),  # noqa: B008
    admin=Depends(require_admin),  # noqa: B008
) -> Response:
    golfers = list_golfers(session)
    return _templates(request).TemplateResponse(
        request, "admin/roster/list.html", {"golfers": golfers}
    )


@router.get("/admin/golfers/new")
async def new_golfer_form(
    request: Request,
    session: Session = Depends(get_session),  # noqa: B008
    admin=Depends(require_admin),  # noqa: B008
) -> Response:
    return _templates(request).TemplateResponse(
        request, "admin/roster/form.html", _golfer_form_context(request, session)
    )


@router.post("/admin/golfers/new")
async def create_golfer_submit(
    request: Request,
    first_name: str = Form(""),
    last_name: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    default_tee_set_id: str = Form(""),
    handicap_strokes: str = Form(""),
    notes: str = Form(""),
    csrf_token: str = Form(""),
    session: Session = Depends(get_session),  # noqa: B008
    admin=Depends(require_admin),  # noqa: B008
) -> Response:
    _require_csrf(request, csrf_token)

    try:
        golfer = create_golfer(
            session,
            first_name=first_name,
            last_name=last_name,
            default_tee_set_id=default_tee_set_id,
            email=email or None,
            phone=phone or None,
            handicap_strokes=handicap_strokes,
            notes=notes or None,
        )
    except RosterValidationError as exc:
        return _templates(request).TemplateResponse(
            request,
            "admin/roster/form.html",
            _golfer_form_context(request, session, errors=exc.errors),
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    return RedirectResponse(
        url=f"/admin/golfers/{golfer.id}/edit", status_code=status.HTTP_303_SEE_OTHER
    )


@router.get("/admin/golfers/{golfer_id}/edit")
async def edit_golfer_form(
    golfer_id: int,
    request: Request,
    session: Session = Depends(get_session),  # noqa: B008
    admin=Depends(require_admin),  # noqa: B008
) -> Response:
    golfer = get_golfer(session, golfer_id)
    if golfer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return _templates(request).TemplateResponse(
        request,
        "admin/roster/form.html",
        _golfer_form_context(request, session, golfer=golfer),
    )


@router.post("/admin/golfers/{golfer_id}/edit")
async def update_golfer_submit(
    golfer_id: int,
    request: Request,
    first_name: str = Form(""),
    last_name: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    default_tee_set_id: str = Form(""),
    handicap_strokes: str = Form(""),
    notes: str = Form(""),
    rendered_tee_set_id: str = Form(""),
    csrf_token: str = Form(""),
    session: Session = Depends(get_session),  # noqa: B008
    admin=Depends(require_admin),  # noqa: B008
) -> Response:
    _require_csrf(request, csrf_token)

    existing = get_golfer(session, golfer_id)
    if existing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    # Changing the tee is a two-step edit: the service rule that a tee
    # change must resupply the handicap cannot fire from a browser on its
    # own, because the form always pre-populates the handicap field with
    # some value. `rendered_tee_set_id` names the tee the form was
    # rendered with; when it does not match the submitted tee (including
    # when it is missing, empty or non-numeric), nothing is written and the
    # form comes back asking for the handicap at the newly chosen tee.
    if not _tee_selection_confirmed(rendered_tee_set_id, default_tee_set_id):
        new_tee_set_id = _try_parse_int(default_tee_set_id)
        return _templates(request).TemplateResponse(
            request,
            "admin/roster/form.html",
            _golfer_form_context(
                request,
                session,
                golfer=existing,
                errors={
                    "handicap_strokes": (
                        "Enter the handicap at the new tee, or leave blank "
                        "if none is on file."
                    )
                },
                selected_tee_set_id=new_tee_set_id,
                rendered_tee_set_id=(
                    new_tee_set_id if new_tee_set_id is not None else ""
                ),
                handicap_display="",
            ),
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    try:
        update_golfer(
            session,
            golfer_id,
            first_name=first_name,
            last_name=last_name,
            default_tee_set_id=default_tee_set_id,
            email=email or None,
            phone=phone or None,
            handicap_strokes=handicap_strokes,
            notes=notes or None,
        )
    except RosterValidationError as exc:
        return _templates(request).TemplateResponse(
            request,
            "admin/roster/form.html",
            _golfer_form_context(
                request, session, golfer=existing, errors=exc.errors
            ),
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    return RedirectResponse(
        url=f"/admin/golfers/{golfer_id}/edit", status_code=status.HTTP_303_SEE_OTHER
    )
