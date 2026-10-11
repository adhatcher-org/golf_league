"""Browser-facing error pages.

FastAPI answers `HTTPException` and request-validation errors with JSON.
That stays the answer for API-style clients. A browser (an `Accept`
header that names `text/html`) gets a styled HTML page instead, with the
same status code and the exception's own headers (for example the
no-store/no-referrer headers on token pages), a plain-language message
and a link to a sensible next step. Nothing here changes which status a
route returns.
"""

from fastapi import FastAPI, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

_PAGES = {
    401: ("Please log in", "You need to log in to see this page.",
          [("/login", "Log in")]),
    403: ("Not allowed", "Your account does not have access to this page.",
          [("/", "Go to the home page")]),
    404: ("Page not found", "That page does not exist, or the link has expired or was already used.",
          [("/", "Go to the home page"), ("/login", "Log in")]),
    405: ("Not allowed", "That action is not available here.",
          [("/", "Go to the home page")]),
    409: ("That change could not be made", "Something changed since this page was loaded. "
          "Go back, reload the page and try again.", [("/", "Go to the home page")]),
    422: ("Check your request", "Part of the request was not valid. Go back and check the form.",
          [("/", "Go to the home page")]),
    503: ("Temporarily unavailable", "The service is not ready yet. Try again in a moment.",
          [("/", "Go to the home page")]),
}
_UNVERIFIED = ("Verify your email address",
               "Your account exists, but its email address is not verified yet. Open the "
               "verification link we emailed you, then log in again.",
               [("/login", "Log in again")])
# Detail strings written for people; anything else (dicts, internal text) is not shown.
_SHOWN_DETAIL_STATUSES = {409, 503}


def wants_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "").lower()


def _page(request: Request, status_code: int, detail, headers) -> object:
    if status_code == 403 and detail == "Email verification required":
        title, message, links = _UNVERIFIED
    else:
        title, message, links = _PAGES.get(status_code, ("Something went wrong",
                                                         "The request could not be completed.",
                                                         [("/", "Go to the home page")]))
        if status_code in _SHOWN_DETAIL_STATUSES and isinstance(detail, str) and detail:
            message = detail
    return request.app.state.templates.TemplateResponse(
        request,
        "error.html",
        {"status_code": status_code, "title": title, "message": message,
         "links": [{"href": href, "label": label} for href, label in links]},
        status_code=status_code,
        headers=dict(headers or {}),
    )


async def _http_exception(request: Request, exc: StarletteHTTPException):
    if wants_html(request) and exc.status_code in _PAGES:
        return _page(request, exc.status_code, exc.detail, getattr(exc, "headers", None))
    return await http_exception_handler(request, exc)


async def _validation_exception(request: Request, exc: RequestValidationError):
    if wants_html(request):
        return _page(request, 422, None, None)
    return await request_validation_exception_handler(request, exc)


def install_error_pages(app: FastAPI) -> None:
    app.add_exception_handler(StarletteHTTPException, _http_exception)
    app.add_exception_handler(RequestValidationError, _validation_exception)
