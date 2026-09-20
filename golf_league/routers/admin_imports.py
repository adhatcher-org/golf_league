"""Admin routes for CSV roster import staging.

HTTP only: every database access goes through
`golf_league.services.roster_imports` (and, for the course picker,
`golf_league.services.courses`). `golf_league.models` is never imported
here.
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
from golf_league.services.courses import get_course, list_courses_with_tee_sets
from golf_league.services.roster_imports import (
    ImportValidationError,
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
    courses = list_courses_with_tee_sets(session)
    return {
        "courses": courses,
        "preselected_course_id": courses[0].id if len(courses) == 1 else None,
        "errors": errors,
        "csrf_token": generate_csrf_token(seed) if seed else "",
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
    course_id: str = Form(""),
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
            course_id=course_id,
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
    batch = get_batch(session, batch_id)
    if batch is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    course = get_course(session, batch.course_id)
    rows = review_rows(session, batch_id)
    return _templates(request).TemplateResponse(
        request,
        "admin/imports/review.html",
        {"batch": batch, "course": course, "rows": rows},
    )


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
