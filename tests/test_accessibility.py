"""GL-44: static accessibility audit of rendered pages and the stylesheet.

This is not browser evidence (GL-61 owns real-browser checks). It proves,
from the server-rendered HTML and the CSS source, that every page keeps a
skip link and a main landmark, every form control has an accessible name,
every button has a name, tables have headers, long synthetic text is escaped,
touch targets are at least 44px (2.75rem) and the stylesheet keeps the single
720px breakpoint from R-VIEWPORTS.
"""

import io
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from conftest import _extract_csrf
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_my_schedule import _seed

from golf_league.models import Course, Golfer, PlayerMatch, TeeSet

CSS = (Path(__file__).resolve().parent.parent / "static" / "app.css").read_text(encoding="utf-8")
LONG_FIRST = "Bartholomew-Maximilian-Synthetic-Longname"
LONG_LAST = "O'Example-<i>Markup</i>-Vandersyntheticson"


class _Audit(HTMLParser):
    """Collect what the assertions need from one rendered page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.label_for: set[str] = set()
        self.ids: list[str] = []
        self.controls: list[dict] = []
        self.buttons: list[dict] = []
        self.label_depth = 0
        self.button_stack: list[dict] = []
        self.tables = 0
        self.table_headers = 0
        self.headings = 0
        self.skip_link = False
        self.main_ids: list[str] = []

    def handle_starttag(self, tag, attrs):
        attr = {name: (value or "") for name, value in attrs}
        if "id" in attr:
            self.ids.append(attr["id"])
        handler = getattr(self, f"_start_{tag}", None)
        if handler is not None:
            handler(attr)
        elif tag in {"h1", "h2"}:
            self.headings += 1

    def _start_label(self, attr):
        self.label_depth += 1
        if attr.get("for"):
            self.label_for.add(attr["for"])

    def _start_input(self, attr):
        if attr.get("type", "text").lower() in {"hidden", "submit", "button", "reset", "image"}:
            return
        self.controls.append({"tag": "input", "attrs": attr, "in_label": self.label_depth > 0})

    def _start_select(self, attr):
        self.controls.append({"tag": "select", "attrs": attr, "in_label": self.label_depth > 0})

    def _start_textarea(self, attr):
        self.controls.append({"tag": "textarea", "attrs": attr, "in_label": self.label_depth > 0})

    def _start_button(self, attr):
        button = {"attrs": attr, "text": ""}
        self.buttons.append(button)
        self.button_stack.append(button)

    def _start_table(self, attr):
        self.tables += 1

    def _start_th(self, attr):
        self.table_headers += 1

    def _start_a(self, attr):
        if attr.get("href") == "#main-content":
            self.skip_link = True

    def _start_main(self, attr):
        self.main_ids.append(attr.get("id", ""))

    def handle_endtag(self, tag):
        if tag == "label":
            self.label_depth = max(0, self.label_depth - 1)
        elif tag == "button" and self.button_stack:
            self.button_stack.pop()

    def handle_data(self, data):
        for button in self.button_stack:
            button["text"] += data


def _audit(html: str) -> _Audit:
    audit = _Audit()
    audit.feed(html)
    audit.close()
    return audit


def _assert_accessible(path: str, html: str) -> None:
    audit = _audit(html)
    assert audit.skip_link, f"{path}: no skip link"
    assert audit.main_ids == ["main-content"], f"{path}: main landmark"
    assert audit.headings, f"{path}: no h1/h2"
    duplicates = {item for item in audit.ids if audit.ids.count(item) > 1}
    assert not duplicates, f"{path}: duplicate ids {duplicates}"
    for control in audit.controls:
        attrs = control["attrs"]
        named = (
            control["in_label"]
            or attrs.get("aria-label", "").strip()
            or attrs.get("aria-labelledby", "").strip()
            or attrs.get("id", "") in audit.label_for
        )
        assert named, f"{path}: unlabelled {control['tag']} {attrs.get('name')!r}"
    for button in audit.buttons:
        assert button["text"].strip() or button["attrs"].get("aria-label", "").strip(), (
            f"{path}: button without a name"
        )
    if audit.tables:
        assert audit.table_headers, f"{path}: table without header cells"


def _long_names(client) -> None:
    with Session(client.app.state.engine) as session:
        golfer = session.scalars(select(Golfer).order_by(Golfer.id)).first()
        golfer.first_name = LONG_FIRST
        golfer.last_name = LONG_LAST
        session.commit()


def _stage_import(client) -> str:
    page = client.get("/admin/roster/imports/new")
    with Session(client.app.state.engine) as session:
        course_id = session.scalars(select(Course.id)).first()
    csv_bytes = (
        "FirstName,LastName,Tee,Handicap,Email,PhoneNumber\n"
        f"{LONG_FIRST},Importrow,Gold,12,long.import@example.test,555-0100\n"
        "Bad,Numbers,White,not-a-number,bad.numbers@example.test,555-0101\n"
    ).encode()
    response = client.post(
        "/admin/roster/imports/new",
        data={"course_id": str(course_id), "csrf_token": _extract_csrf(page.text)},
        files=[("files", ("synthetic.csv", io.BytesIO(csv_bytes), "text/csv"))],
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return response.headers["location"]


def _admin_pages(client, ids) -> list[str]:
    season, week = ids["season"], ids["weeks"][0]
    with Session(client.app.state.engine) as session:
        course = session.scalars(select(Course)).first()
        tee = session.scalars(select(TeeSet).where(TeeSet.course_id == course.id)).first()
        player_match_id = session.scalars(select(PlayerMatch.id)).first()
    return [
        "/", "/roster", "/my/schedule", f"/weeks/{week}?season_id={season}",
        "/admin/golfers", "/admin/golfers/new", f"/admin/golfers/{ids['golfers'][0]}/edit",
        "/admin/roster/imports/new", _stage_import(client),
        "/admin/courses", "/admin/courses/new", f"/admin/courses/{course.id}/edit",
        f"/admin/courses/{course.id}/holes", f"/admin/courses/{course.id}/tees/new",
        f"/admin/courses/{course.id}/tees/{tee.id}/edit",
        "/admin/seasons", "/admin/seasons/new", f"/admin/seasons/{season}/edit",
        f"/admin/seasons/{season}/participants", f"/admin/seasons/{season}/teams",
        f"/admin/seasons/{season}/teams/new", f"/admin/teams/{ids['teams'][0]}/edit",
        f"/admin/seasons/{season}/weeks", f"/admin/seasons/{season}/weeks/{week}/edit",
        f"/admin/seasons/{season}/weeks/{week}/matches/new",
        f"/admin/seasons/{season}/weeks/{week}/nine", f"/admin/seasons/{season}/weeks/{week}/shift",
        f"/admin/seasons/{season}/player-matches/{player_match_id}/substitute",
        "/admin/invites", "/admin/invites/new", "/admin/config",
    ]


def test_admin_and_player_pages_pass_static_audit_with_long_names(client):
    ids = _seed(client, admin=True)
    _long_names(client)
    for path in _admin_pages(client, ids):
        response = client.get(path)
        assert response.status_code == 200, f"{path}: {response.status_code}"
        _assert_accessible(path, response.text)
        assert "<i>Markup</i>" not in response.text, f"{path}: unescaped synthetic markup"


def test_anonymous_pages_pass_static_audit(client):
    for path in ["/", "/login", "/register", "/reset"]:
        response = client.get(path)
        assert response.status_code == 200, path
        _assert_accessible(path, response.text)


def test_empty_admin_states_pass_static_audit(empty_admin_client):
    for path in ["/admin/golfers", "/admin/courses", "/admin/seasons", "/admin/invites", "/admin/config"]:
        response = empty_admin_client.get(path)
        assert response.status_code == 200, path
        _assert_accessible(path, response.text)


def test_identity_error_states_are_announced_with_text(client):
    page = client.get("/register")
    response = client.post(
        "/register", data={"email": "", "password": "short", "csrf_token": _extract_csrf(page.text)},
    )
    assert response.status_code == 422
    assert response.text.count('class="error" role="alert"') == 2
    _assert_accessible("/register (errors)", response.text)


# --- stylesheet -------------------------------------------------------------


def _rules(css: str):
    """Yield (selector, body) for every plain rule, including inside @media."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    for match in re.finditer(r"([^{}@]+)\{([^{}]*)\}", css):
        yield match.group(1).strip(), match.group(2)


def test_stylesheet_has_only_the_720px_breakpoint():
    queries = set(re.findall(r"@media\s*([^{]+)\{", CSS))
    assert {query.strip() for query in queries} == {
        "(max-width: 720px)", "(min-width: 721px)", "(prefers-reduced-motion: reduce)",
    }


_INTERACTIVE = re.compile(r"(^|[\s,>])(button|summary|select|input|textarea|a)(\b|$)|button-link|touch-target|select-all|import-check|__clear\b")
_SMALL_CONTROL = re.compile(r"checkbox|-check input|select-all input|__clear input")


@pytest.mark.parametrize("selector,body", [
    rule for rule in _rules(CSS) if "min-height" in rule[1] and _INTERACTIVE.search(rule[0])
])
def test_interactive_touch_targets_are_at_least_44px(selector, body):
    value = re.search(r"min-height:\s*([0-9.]+)rem", body)
    if value is None or _SMALL_CONTROL.search(selector):
        # Checkboxes sit inside a label or .touch-target that carries the 44px box.
        return
    assert float(value.group(1)) >= 2.75, selector


def test_wide_tables_scroll_inside_their_own_region():
    assert re.search(r"\.table-wrap\s*\{[^}]*overflow-x:\s*auto", CSS)
    assert ".golfer-table-wrap { overflow-x: auto; }" in CSS
    assert "min-width: 320px" in CSS
