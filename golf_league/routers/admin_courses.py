"""Admin routes for courses, tee sets and tee ratings.

Routes only: every database access goes through
`golf_league.services.courses`. `golf_league.models` is never imported
here.
"""

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from golf_league.database import get_session
from golf_league.security import generate_csrf_token, require_admin, validate_csrf
from golf_league.services.courses import (
    CourseValidationError,
    create_course,
    create_tee_set_with_ratings,
    get_course,
    get_tee_set_for_course,
    list_courses_with_tee_sets,
    update_course,
    update_tee_set_with_ratings,
)

router = APIRouter()

SESSION_COOKIE_NAME = "session"
SCOPES = ("front", "back", "full")


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


def _course_form_context(request: Request, *, course=None, errors=None) -> dict:
    seed = _csrf_seed(request)
    return {
        "course": course,
        "errors": errors,
        "csrf_token": generate_csrf_token(seed) if seed else "",
    }


def _tee_form_context(request: Request, *, course_id: int, tee_set=None, ratings=None, errors=None) -> dict:
    seed = _csrf_seed(request)
    return {
        "course_id": course_id,
        "tee_set": tee_set,
        "ratings": ratings or {},
        "errors": errors,
        "csrf_token": generate_csrf_token(seed) if seed else "",
    }


def _ratings_from_form(
    front_rating: str,
    front_slope: str,
    front_par: str,
    back_rating: str,
    back_slope: str,
    back_par: str,
    full_rating: str,
    full_slope: str,
    full_par: str,
) -> dict[str, dict[str, object]]:
    return {
        "front": {"rating": front_rating, "slope": front_slope, "par": front_par},
        "back": {"rating": back_rating, "slope": back_slope, "par": back_par},
        "full": {"rating": full_rating, "slope": full_slope, "par": full_par},
    }


@router.get("/admin/courses")
async def list_courses(
    request: Request,
    session: Session = Depends(get_session),  # noqa: B008
    admin=Depends(require_admin),  # noqa: B008
) -> Response:
    courses = list_courses_with_tee_sets(session)
    return _templates(request).TemplateResponse(
        request, "admin/courses/list.html", {"courses": courses}
    )


@router.get("/admin/courses/new")
async def new_course_form(
    request: Request,
    admin=Depends(require_admin),  # noqa: B008
) -> Response:
    return _templates(request).TemplateResponse(
        request, "admin/courses/form.html", _course_form_context(request)
    )


@router.post("/admin/courses/new")
async def create_course_submit(
    request: Request,
    name: str = Form(...),
    city: str = Form(""),
    state: str = Form(""),
    website: str = Form(""),
    total_holes: str = Form("18"),
    csrf_token: str = Form(...),
    session: Session = Depends(get_session),  # noqa: B008
    admin=Depends(require_admin),  # noqa: B008
) -> Response:
    _require_csrf(request, csrf_token)

    try:
        course = create_course(
            session,
            name=name,
            city=city or None,
            state=state or None,
            website=website or None,
            total_holes=total_holes,
        )
    except CourseValidationError as exc:
        return _templates(request).TemplateResponse(
            request,
            "admin/courses/form.html",
            _course_form_context(request, errors=exc.errors),
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    return RedirectResponse(
        url=f"/admin/courses/{course.id}/edit", status_code=status.HTTP_303_SEE_OTHER
    )


@router.get("/admin/courses/{course_id}/edit")
async def edit_course_form(
    course_id: int,
    request: Request,
    session: Session = Depends(get_session),  # noqa: B008
    admin=Depends(require_admin),  # noqa: B008
) -> Response:
    course = get_course(session, course_id)
    if course is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return _templates(request).TemplateResponse(
        request, "admin/courses/form.html", _course_form_context(request, course=course)
    )


@router.post("/admin/courses/{course_id}/edit")
async def update_course_submit(
    course_id: int,
    request: Request,
    name: str = Form(...),
    city: str = Form(""),
    state: str = Form(""),
    website: str = Form(""),
    total_holes: str = Form("18"),
    csrf_token: str = Form(...),
    session: Session = Depends(get_session),  # noqa: B008
    admin=Depends(require_admin),  # noqa: B008
) -> Response:
    _require_csrf(request, csrf_token)

    existing = get_course(session, course_id)
    if existing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    try:
        update_course(
            session,
            course_id,
            name=name,
            city=city or None,
            state=state or None,
            website=website or None,
            total_holes=total_holes,
        )
    except CourseValidationError as exc:
        return _templates(request).TemplateResponse(
            request,
            "admin/courses/form.html",
            _course_form_context(request, course=existing, errors=exc.errors),
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    return RedirectResponse(
        url=f"/admin/courses/{course_id}/edit", status_code=status.HTTP_303_SEE_OTHER
    )


@router.get("/admin/courses/{course_id}/tees/new")
async def new_tee_form(
    course_id: int,
    request: Request,
    session: Session = Depends(get_session),  # noqa: B008
    admin=Depends(require_admin),  # noqa: B008
) -> Response:
    course = get_course(session, course_id)
    if course is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return _templates(request).TemplateResponse(
        request,
        "admin/courses/tee_form.html",
        _tee_form_context(request, course_id=course_id),
    )


@router.post("/admin/courses/{course_id}/tees/new")
async def create_tee_submit(
    course_id: int,
    request: Request,
    name: str = Form(...),
    color_label: str = Form(...),
    gender: str = Form(...),
    total_yards: str = Form(...),
    sort_order: str = Form(...),
    front_rating: str = Form(""),
    front_slope: str = Form(""),
    front_par: str = Form(""),
    back_rating: str = Form(""),
    back_slope: str = Form(""),
    back_par: str = Form(""),
    full_rating: str = Form(""),
    full_slope: str = Form(""),
    full_par: str = Form(""),
    csrf_token: str = Form(...),
    session: Session = Depends(get_session),  # noqa: B008
    admin=Depends(require_admin),  # noqa: B008
) -> Response:
    _require_csrf(request, csrf_token)

    course = get_course(session, course_id)
    if course is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    ratings = _ratings_from_form(
        front_rating, front_slope, front_par,
        back_rating, back_slope, back_par,
        full_rating, full_slope, full_par,
    )

    try:
        create_tee_set_with_ratings(
            session,
            course_id,
            name=name,
            color_label=color_label,
            gender=gender,
            total_yards=total_yards,
            sort_order=sort_order,
            ratings=ratings,
        )
    except CourseValidationError as exc:
        return _templates(request).TemplateResponse(
            request,
            "admin/courses/tee_form.html",
            _tee_form_context(request, course_id=course_id, ratings=ratings, errors=exc.errors),
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    return RedirectResponse(
        url=f"/admin/courses/{course_id}/edit", status_code=status.HTTP_303_SEE_OTHER
    )


@router.get("/admin/courses/{course_id}/tees/{tee_set_id}/edit")
async def edit_tee_form(
    course_id: int,
    tee_set_id: int,
    request: Request,
    session: Session = Depends(get_session),  # noqa: B008
    admin=Depends(require_admin),  # noqa: B008
) -> Response:
    tee_set = get_tee_set_for_course(session, course_id, tee_set_id)
    if tee_set is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    ratings = {
        rating.scope: {"rating": rating.rating, "slope": rating.slope, "par": rating.par}
        for rating in tee_set.ratings
    }
    return _templates(request).TemplateResponse(
        request,
        "admin/courses/tee_form.html",
        _tee_form_context(request, course_id=course_id, tee_set=tee_set, ratings=ratings),
    )


@router.post("/admin/courses/{course_id}/tees/{tee_set_id}/edit")
async def update_tee_submit(
    course_id: int,
    tee_set_id: int,
    request: Request,
    name: str = Form(...),
    color_label: str = Form(...),
    gender: str = Form(...),
    total_yards: str = Form(...),
    sort_order: str = Form(...),
    front_rating: str = Form(""),
    front_slope: str = Form(""),
    front_par: str = Form(""),
    back_rating: str = Form(""),
    back_slope: str = Form(""),
    back_par: str = Form(""),
    full_rating: str = Form(""),
    full_slope: str = Form(""),
    full_par: str = Form(""),
    csrf_token: str = Form(...),
    session: Session = Depends(get_session),  # noqa: B008
    admin=Depends(require_admin),  # noqa: B008
) -> Response:
    _require_csrf(request, csrf_token)

    existing = get_tee_set_for_course(session, course_id, tee_set_id)
    if existing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    ratings = _ratings_from_form(
        front_rating, front_slope, front_par,
        back_rating, back_slope, back_par,
        full_rating, full_slope, full_par,
    )

    try:
        update_tee_set_with_ratings(
            session,
            course_id,
            tee_set_id,
            name=name,
            color_label=color_label,
            gender=gender,
            total_yards=total_yards,
            sort_order=sort_order,
            ratings=ratings,
        )
    except CourseValidationError as exc:
        return _templates(request).TemplateResponse(
            request,
            "admin/courses/tee_form.html",
            _tee_form_context(
                request, course_id=course_id, tee_set=existing, ratings=ratings, errors=exc.errors
            ),
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    return RedirectResponse(
        url=f"/admin/courses/{course_id}/edit", status_code=status.HTTP_303_SEE_OTHER
    )
