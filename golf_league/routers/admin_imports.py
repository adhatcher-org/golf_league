"""Admin routes for CSV roster import staging.

HTTP only: every database access goes through
`golf_league.services.roster_imports`. `golf_league.models` is never
imported here.
"""

from datetime import UTC, datetime

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from golf_league.database import get_session
from golf_league.security import generate_csrf_token, require_admin, validate_csrf
from golf_league.services.roster_imports import (
    ImportApplyError,
    ImportConflictError,
    ImportNotFoundError,
    ImportValidationError,
    apply_batch,
    discard_batch,
    edit_staged_row,
    get_batch,
    purge_expired,
    review_rows,
    stage_batch,
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


def _new_form_context(request: Request, session: Session, *, errors=None) -> dict:
    seed = _csrf_seed(request)
    return {
        "errors": errors,
        "csrf_token": generate_csrf_token(seed) if seed else "",
    }


def _review_context(request: Request, session: Session, batch_id: int, *, errors=None) -> dict:
    batch = get_batch(session, batch_id)
    if batch is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return {
        "batch": batch,
        "rows": review_rows(session, batch_id),
        "errors": errors or {},
        "csrf_token": generate_csrf_token(_csrf_seed(request)),
    }


@router.get("/admin/roster/imports/new")
async def new_import_form(
    request: Request,
    session: Session = Depends(get_session),  # noqa: B008
    admin=Depends(require_admin),  # noqa: B008
) -> Response:
    return _templates(request).TemplateResponse(
        request, "admin/imports/new.html", _new_form_context(request, session)
    )


@router.post("/admin/roster/imports/new")
async def create_import(
    request: Request,
    csrf_token: str = Form(""),
    files: list[UploadFile] = File(default=[]),  # noqa: B008
    session: Session = Depends(get_session),  # noqa: B008
    admin=Depends(require_admin),  # noqa: B008
) -> Response:
    _require_csrf(request, csrf_token)

    file_pairs: list[tuple[str, bytes]] = []
    for upload in files:
        content = await upload.read()
        file_pairs.append((upload.filename or "", content))

    now = datetime.now(UTC).replace(tzinfo=None)

    try:
        batch_id = stage_batch(
            session,
            created_by_user_id=admin.id,
            files=file_pairs,
            now=now,
        )
    except ImportValidationError as exc:
        return _templates(request).TemplateResponse(
            request,
            "admin/imports/new.html",
            _new_form_context(request, session, errors=exc.errors),
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    return RedirectResponse(
        url=f"/admin/roster/imports/{batch_id}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.get("/admin/roster/imports/{batch_id}")
async def review_import(
    batch_id: int,
    request: Request,
    session: Session = Depends(get_session),  # noqa: B008
    admin=Depends(require_admin),  # noqa: B008
) -> Response:
    return _templates(request).TemplateResponse(
        request, "admin/imports/review.html", _review_context(request, session, batch_id)
    )


@router.post("/admin/roster/imports/{batch_id}/rows/{row_id}/edit")
async def edit_import_row(
    batch_id: int, row_id: int, request: Request,
    csrf_token: str = Form(""), batch_version: int = Form(...), row_version: int = Form(...),
    action: str = Form("edit"), first_name: str = Form(""), last_name: str = Form(""),
    email: str = Form(""), phone: str = Form(""), tee_label: str = Form(""),
    handicap_gold: str = Form(""), handicap_white: str = Form(""), handicap_single: str = Form(""),
    included: bool = Form(False), update_opt_in: bool = Form(False),
    session: Session = Depends(get_session), admin=Depends(require_admin),  # noqa: B008
) -> Response:
    _require_csrf(request, csrf_token)
    try:
        edit_staged_row(session, batch_id=batch_id, batch_version=batch_version,
            row_id=row_id, row_version=row_version, action=action, first_name=first_name,
            last_name=last_name, email=email, phone=phone, tee_label=tee_label,
            handicap_gold=handicap_gold, handicap_white=handicap_white,
            handicap_single=handicap_single, included=included, update_opt_in=update_opt_in)
    except ImportNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Not found"
        ) from None
    except ImportConflictError:
        return _templates(request).TemplateResponse(request, "admin/imports/review.html",
            _review_context(request, session, batch_id, errors={"conflict": "This review changed; use the refreshed version."}), status_code=409)
    except ImportValidationError as exc:
        return _templates(request).TemplateResponse(request, "admin/imports/review.html",
            _review_context(request, session, batch_id, errors=exc.errors), status_code=422)
    return RedirectResponse(url=f"/admin/roster/imports/{batch_id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/admin/roster/imports/{batch_id}/discard")
async def discard_import(
    batch_id: int, request: Request, csrf_token: str = Form(""), batch_version: int = Form(...),
    session: Session = Depends(get_session), admin=Depends(require_admin),  # noqa: B008
) -> Response:
    _require_csrf(request, csrf_token)
    try:
        discard_batch(session, batch_id=batch_id, batch_version=batch_version)
    except ImportConflictError:
        return _templates(request).TemplateResponse(request, "admin/imports/review.html",
            _review_context(request, session, batch_id, errors={"conflict": "This review changed; use the refreshed version."}), status_code=409)
    return RedirectResponse(url=f"/admin/roster/imports/{batch_id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/admin/roster/imports/{batch_id}/apply")
async def apply_import(
    batch_id: int, request: Request, csrf_token: str = Form(""), batch_version: int = Form(...),
    session: Session = Depends(get_session), admin=Depends(require_admin),  # noqa: B008
) -> Response:
    _require_csrf(request, csrf_token)
    try:
        apply_batch(session, batch_id=batch_id, batch_version=batch_version,
            now=datetime.now(UTC).replace(tzinfo=None))
    except ImportConflictError:
        return _templates(request).TemplateResponse(request, "admin/imports/review.html",
            _review_context(request, session, batch_id, errors={"conflict": "This review changed; use the refreshed version."}), status_code=409)
    except ImportApplyError:
        return _templates(request).TemplateResponse(request, "admin/imports/review.html",
            _review_context(request, session, batch_id, errors={"apply": "Included rows must have no errors before applying."}), status_code=422)
    return RedirectResponse(url=f"/admin/roster/imports/{batch_id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/admin/roster/imports/purge")
async def purge_import(
    request: Request,
    csrf_token: str = Form(""),
    session: Session = Depends(get_session),  # noqa: B008
    admin=Depends(require_admin),  # noqa: B008
) -> Response:
    _require_csrf(request, csrf_token)
    now = datetime.now(UTC).replace(tzinfo=None)
    purge_expired(session, now=now)
    return RedirectResponse(
        url="/admin/roster/imports/new", status_code=status.HTTP_303_SEE_OTHER
    )
