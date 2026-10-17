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
        self.h1 = 0
        self.lang = ""
        self.stylesheets: list[str] = []
        self.container_stack: list[str] = []
        self.unwrapped_tables = 0

    def handle_starttag(self, tag, attrs):
        attr = {name: (value or "") for name, value in attrs}
        if "id" in attr:
            self.ids.append(attr["id"])
        if tag in {"div", "section"}:
            self.container_stack.append(attr.get("class", ""))
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
        wrappers = ("table-wrap", "season-roster__table-wrap")
        if not any(name in classes for classes in self.container_stack for name in wrappers):
            self.unwrapped_tables += 1

    def _start_th(self, attr):
        self.table_headers += 1

    def _start_a(self, attr):
        if attr.get("href") == "#main-content":
            self.skip_link = True

    def _start_h1(self, attr):
        self.h1 += 1
        self.headings += 1

    def _start_html(self, attr):
        self.lang = attr.get("lang", "")

    def _start_link(self, attr):
        if attr.get("rel") == "stylesheet":
            self.stylesheets.append(attr.get("href", ""))

    def _start_main(self, attr):
        self.main_ids.append(attr.get("id", ""))

    def handle_endtag(self, tag):
        if tag in {"div", "section"} and self.container_stack:
            self.container_stack.pop()
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
    assert audit.h1 == 1, f"{path}: expected exactly one h1, found {audit.h1}"
    assert audit.lang == "en", f"{path}: <html> has no lang"
    assert any(href.startswith("/static/app.css") for href in audit.stylesheets), f"{path}: no stylesheet"
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
        assert audit.unwrapped_tables == 0, f"{path}: table outside a scrolling .table-wrap region"


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


def test_scorecard_pages_pass_static_audit_with_long_names(client):
    """GL-82: every scorecard page, in each of its states, at the same audit as the rest."""
    from datetime import UTC, datetime

    from test_admin_scorecards import (
        DeferredExecutor,
        FakeProvider,
        InlineExecutor,
        _card,
        _draft_id,
        _player,
        _submit,
        _upload,
    )

    from golf_league.services.scorecard_vision import ProviderTimeout

    ids = _seed(client, admin=True)
    _long_names(client)
    week, golfers, state = ids["weeks"][0], ids["golfers"], client.app.state
    state.scorecard_utc_now = lambda: datetime(2026, 8, 27, 18, 0, tzinfo=UTC)
    pages: list[tuple[str, str]] = []

    def audit(label: str, response, status: int = 200) -> None:
        assert response.status_code == status, f"{label}: {response.status_code}"
        pages.append((label, response.text))

    state.vision_provider = None
    audit("new (photo reading not set up)", client.get("/admin/scorecards/new"))

    # A reading with every kind of note: an unread cell, an unclear cell, a
    # flagged name, a total that disagrees, a wrong-nine header.
    state.vision_provider = FakeProvider(_card(
        _player(LONG_FIRST, (6, 6, 6, 6, 6, None, 6, 5, 5), (True,) + (False,) * 8,
                handicap_written=7, out_total=43),
        _player("B1", out_total=40), _player("A2", (6,) * 9, out_total=44), _player("B2"),
        header=tuple(range(10, 19)),
    ))
    state.scorecard_executor = DeferredExecutor()
    waiting = _draft_id(_upload(client, week))
    audit("status (being read)", client.get(f"/admin/scorecards/{waiting}"))
    audit("new (with a card in progress)", client.get("/admin/scorecards/new"))
    state.scorecard_executor.run()
    review = client.get(f"/admin/scorecards/{waiting}")
    audit("review (what was read)", review)
    assert "Not read" in review.text and "Unclear" in review.text and "Check before you submit" in review.text

    rows = [(LONG_FIRST, golfers[0], ("x",) * 9), ("B1", "", (5,) * 9), ("A2", golfers[1], (5,) * 9)]
    audit("review (errors)", _submit(client, waiting, rows), 422)
    rows = [(LONG_FIRST, golfers[0], (5,) * 9), ("B1", golfers[4], (5,) * 9), ("A2", golfers[1], (5,) * 9)]
    audit("review (acknowledgement needed)", _submit(client, waiting, rows), 422)

    state.scorecard_executor = InlineExecutor()
    state.vision_provider = FakeProvider(ProviderTimeout())
    failed = _draft_id(_upload(client, week))
    audit("failed", client.get(f"/admin/scorecards/{failed}"))
    audit("review (manual, after a failed read)", client.get(f"/admin/scorecards/{failed}?manual=1"))

    audit("scores (none saved)", client.get(f"/admin/weeks/{week}/scores"))
    saved = _submit(client, waiting, rows, acknowledged=[golfers[1]])
    assert saved.status_code == 303
    audit("scores (a saved card)", client.get(saved.headers["location"]))
    audit("new (with saved scores)", client.get("/admin/scorecards/new"))

    for label, html in pages:
        _assert_accessible(label, html)
        assert "<i>Markup</i>" not in html, f"{label}: unescaped synthetic markup"
    assert len(pages) == 11


def test_week_score_pages_pass_static_audit_with_long_names(client):
    """GL-84: the week sheet, the match page (open, locked) and the clear page, in each state."""
    from datetime import UTC, datetime

    from golf_league.security import generate_csrf_token

    ids = _seed(client, admin=True)
    _long_names(client)
    week, state = ids["weeks"][0], client.app.state
    state.scorecard_utc_now = lambda: datetime(2026, 8, 27, 18, 0, tzinfo=UTC)
    with Session(client.app.state.engine) as session:
        matches = list(session.scalars(select(PlayerMatch).order_by(PlayerMatch.id)))
        sides = [(match.id, match.a_golfer_id, match.b_golfer_id) for match in matches]
    token = generate_csrf_token(client.cookies.get("session") or "")
    sheet, base = f"/admin/weeks/{week}/scores", f"/admin/weeks/{week}/matches"
    pages: list[tuple[str, str]] = []

    def audit(label: str, response, status: int = 200) -> None:
        assert response.status_code == status, f"{label}: {response.status_code}"
        pages.append((label, response.text))

    def match_fields(index: int, value: str) -> dict:
        match_id, a, b = sides[index]
        return {f"score_{match_id}_{golfer}_{hole}": value for golfer in (a, b) for hole in range(1, 10)}

    audit("sheet (nothing entered)", client.get(sheet))
    audit("sheet (errors)", client.post(sheet, data={
        "csrf_token": token, f"revision_{sides[0][0]}": "0", **match_fields(0, "x")}), 422)
    saved = client.post(sheet, follow_redirects=False, data={
        "csrf_token": token, f"revision_{sides[0][0]}": "0", f"revision_{sides[1][0]}": "0",
        **match_fields(0, "5"), **match_fields(1, "6")})
    assert saved.status_code == 303
    audit("sheet (saved, with notice)", client.get(saved.headers["location"]))
    audit("match (open, with saved scores)", client.get(f"{base}/{sides[0][0]}/scores"))
    audit("match (empty)", client.get(f"{base}/{sides[2][0]}/scores"))
    audit("match (out of date)", client.post(f"{base}/{sides[0][0]}/scores", data={
        "csrf_token": token, "revision": "0",
        **{f"score_{golfer}_{hole}": "4" for golfer in sides[0][1:] for hole in range(1, 10)}}), 409)
    audit("clear (confirmation)", client.get(f"{base}/{sides[1][0]}/scores/clear"))
    locked = client.post(f"{base}/{sides[0][0]}/lock", data={"csrf_token": token, "revision": "1"},
                         follow_redirects=False)
    assert locked.status_code == 303
    audit("sheet (a locked match)", client.get(locked.headers["location"]))
    audit("match (locked)", client.get(f"{base}/{sides[0][0]}/scores"))
    audit("match (locked, clear refused)", client.get(f"{base}/{sides[0][0]}/scores/clear"), 409)
    lock_all = client.post(f"{sheet}/lock-all", follow_redirects=False, data={
        "csrf_token": token, **{f"revision_{match_id}": "0" for match_id, _, _ in sides}})
    audit("sheet (after lock all, a changed match named)", client.get(lock_all.headers["location"]))

    for label, html in pages:
        _assert_accessible(label, html)
        assert "<i>Markup</i>" not in html, f"{label}: unescaped synthetic markup"
    assert any("Locked on" in html for _, html in pages) and len(pages) == 11


def test_player_score_pages_pass_static_audit_with_long_names(client):
    """GL-85: the week's scores, the entry page and the match form, in each state a player meets."""
    from datetime import UTC, datetime

    from golf_league.models import Week
    from golf_league.security import generate_csrf_token

    ids = _seed(client)  # the viewer is the long-named A1, in week 1 (27 August)
    _long_names(client)
    week, state = ids["weeks"][0], client.app.state
    with Session(client.app.state.engine) as session:
        match = session.scalars(select(PlayerMatch).order_by(PlayerMatch.id)).first()
        match_id, sides = match.id, (match.a_golfer_id, match.b_golfer_id)
    token = generate_csrf_token(client.cookies.get("session") or "")
    enter, scores, form = f"/weeks/{week}/scores/enter", f"/weeks/{week}/scores", f"/weeks/{week}/matches/{match_id}/scores"
    pages: list[tuple[str, str]] = []

    def audit(label: str, response, status: int = 200) -> None:
        assert response.status_code == status, f"{label}: {response.status_code}"
        pages.append((label, response.text))

    def fields(value: str) -> dict:
        return {f"score_{golfer}_{hole}": value for golfer in sides for hole in range(1, 10)}

    state.scorecard_utc_now = lambda: datetime(2026, 8, 26, 18, 0, tzinfo=UTC)
    audit("entry (not open yet)", client.get(enter))
    state.scorecard_utc_now = lambda: datetime(2026, 8, 27, 18, 0, tzinfo=UTC)
    audit("entry (open)", client.get(enter))
    audit("week scores (nothing entered)", client.get(scores))
    audit("match (errors)", client.post(form, data={"csrf_token": token, "revision": "0", **fields("x")}), 422)
    saved = client.post(form, data={"csrf_token": token, "revision": "0", **fields("5")}, follow_redirects=False)
    assert saved.status_code == 303
    audit("week scores (saved, with notice)", client.get(saved.headers["location"]))
    audit("entry (saved)", client.get(enter))
    audit("match (out of date)", client.post(form, data={"csrf_token": token, "revision": "0", **fields("4")}), 409)
    with Session(client.app.state.engine) as session:
        session.get(Week, week).status = "played"
        session.commit()
    audit("entry (closed)", client.get(enter))
    audit("match (closed)", client.get(form))

    for label, html in pages:
        _assert_accessible(label, html)
        assert "<i>Markup</i>" not in html, f"{label}: unescaped synthetic markup"
    assert len(pages) == 9


def test_player_scorecard_pages_pass_static_audit_with_long_names(client):
    """GL-86: the player's entry page with a card offered, and the waiting, failed and review pages."""
    from datetime import UTC, datetime

    from test_admin_scorecards import (
        DeferredExecutor,
        FakeProvider,
        InlineExecutor,
        _card,
        _form,
        _jpeg,
        _player,
    )

    from golf_league.security import generate_csrf_token
    from golf_league.services.scorecard_vision import ProviderTimeout

    ids = _seed(client)  # the viewer is the long-named A1, in week 1 (27 August)
    _long_names(client)
    week, golfers, state = ids["weeks"][0], ids["golfers"], client.app.state
    state.scorecard_utc_now = lambda: datetime(2026, 8, 27, 18, 0, tzinfo=UTC)
    token = generate_csrf_token(client.cookies.get("session") or "")
    pages: list[tuple[str, str]] = []

    def audit(label: str, response, status: int = 200) -> None:
        assert response.status_code == status, f"{label}: {response.status_code}"
        pages.append((label, response.text))

    def upload():
        response = client.post(f"/weeks/{week}/scorecards", data={"csrf_token": token},
                               files={"photo": ("card.jpg", _jpeg(), "image/jpeg")}, follow_redirects=False)
        assert response.status_code == 303, response.text
        return response.headers["location"]

    state.vision_provider = None
    audit("entry (photo reading not set up)", client.get(f"/weeks/{week}/scores/enter"))
    state.vision_provider = FakeProvider(_card(_player(LONG_FIRST), _player("B1")))
    audit("entry (a card offered)", client.get(f"/weeks/{week}/scores/enter"))
    state.scorecard_executor = DeferredExecutor()
    card = upload()
    audit("waiting", client.get(card))
    audit("entry (a card in progress)", client.get(f"/weeks/{week}/scores/enter"))
    state.scorecard_executor.run()
    audit("review (what was read)", client.get(card))
    rows = [(LONG_FIRST, golfers[0], ("x",) * 9), ("B1", golfers[4], (5,) * 9)]
    audit("review (errors)", client.post(f"{card}/submit", data=_form(client, rows)), 422)
    client.post(f"{card}/discard", data={"csrf_token": token})
    state.scorecard_executor = InlineExecutor()
    state.vision_provider = FakeProvider(ProviderTimeout())
    failed = upload()
    audit("failed", client.get(failed))
    audit("review (typed, after a failed read)", client.get(f"{failed}?manual=1"))

    for label, html in pages:
        _assert_accessible(label, html)
        assert "<i>Markup</i>" not in html, f"{label}: unescaped synthetic markup"
    assert len(pages) == 8


def test_score_page_links_keep_a_44px_target_at_every_width():
    """Found in review: inline links are 44px only on phones; the M9 score links must be at both widths."""
    rule = _rule(".tap-link")
    assert "min-height: 2.75rem" in rule and "display: inline-flex" in rule


def test_week_score_layout_rules_keep_the_page_from_scrolling_sideways():
    assert "min-width: 0" in _rule(".score-match")  # the grid scrolls; the block never widens the page
    assert "min-width: 46rem" in _rule(".score-grid--named table")
    assert "overflow-wrap: anywhere" in _rule('.score-grid--named th[scope="row"]')
    assert "flex-wrap: wrap" in _rule(".score-match__actions")


def test_scorecard_layout_rules_keep_the_page_from_scrolling_sideways():
    # Photo beside the form only on a desktop; each player's grid scrolls in its own region.
    assert "grid-template-columns: minmax(0, 1fr)" in _rule(".scorecard-review")
    assert re.search(r"@media \(min-width: 721px\) \{\s*\.scorecard-review \{[^}]*minmax\(0, 2fr\) minmax\(0, 3fr\)", CSS)
    assert "min-width: 0" in _rule(".scorecard-player")  # a fieldset would otherwise grow to its table
    assert "min-width: 0" in _rule(".scorecard-review__photo,\n.scorecard-review__form")
    assert "max-width: 100%" in _rule(".scorecard-review__photo img")
    assert "min-width: 34rem" in _rule(".score-grid table")
    assert "min-height: 2.75rem" in _rule(".score-input")
    assert "min-height: 2.75rem" in _rule(".scorecard-acknowledge")


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


def test_public_token_pages_use_the_styled_layout_and_private_headers(client):
    from test_join import _golfer, _invite

    from golf_league.services.auth import issue_token

    invite = _invite(client)
    _golfer(client)
    join = client.get(f"/join/{invite}")
    client.post(f"/join/{invite}", data={"email": "player@example.test", "csrf_token": _extract_csrf(join.text)})
    body = client.app.state.email_sender.sent[-1]["body"]
    set_password_path = "/set-password/" + body.rsplit("/set-password/", 1)[1]

    with Session(client.app.state.engine) as session:
        from golf_league.models import User

        user_id = session.scalar(select(User.id).where(User.email == "admin@example.test"))
        reset_token = issue_token(session, user_id, "reset_password", 3600)

    for path in (f"/join/{invite}", set_password_path, "/verify/not-a-real-token", f"/reset/{reset_token}"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.headers["cache-control"] == "no-store", path
        assert response.headers["referrer-policy"] == "no-referrer", path
        _assert_accessible(path, response.text)
        assert 'href="/login"' in response.text, path
    missing = client.get("/reset/not-a-real-token")
    assert missing.status_code == 404
    assert missing.headers["cache-control"] == "no-store"
    assert missing.headers["referrer-policy"] == "no-referrer"


def _rule(selector: str, *, media: str | None = None) -> str:
    """Body of the first rule with exactly this selector (optionally inside a media block)."""
    css = re.sub(r"/\*.*?\*/", "", CSS, flags=re.DOTALL)
    scope = css
    if media is not None:
        blocks = [m.end() for m in re.finditer(re.escape(f"@media {media}") + r"\s*\{", css)]
        scope = "".join(css[start:start + 6000] for start in blocks)
    match = re.search(r"(?:^|[{}])\s*" + re.escape(selector) + r"\s*\{([^}]*)\}", scope)
    assert match, selector
    return match.group(1)


def test_hidden_labels_and_wide_content_cannot_widen_the_page():
    hidden = _rule(".visually-hidden")
    assert "top: 0" in hidden and "left: 0" in hidden
    wrap = _rule(".table-wrap")
    assert "position: relative" in wrap and "overflow-x: auto" in wrap
    assert "max-width: 100%" in _rule('input[type="file"]')
    assert "pre-wrap" in _rule(".wrap-pre")


def test_import_review_columns_do_not_use_font_relative_units():
    columns = re.findall(r"grid-template-columns:([^;]*);", _rule(".import-fields__header,\n    .import-fields", media="(min-width: 721px)"))
    assert columns and all("ch" not in value for value in columns)


def test_phone_tap_targets_for_links_brand_and_my_schedule_cards():
    assert "min-height: 2.75rem" in _rule(".brand")
    assert "min-height: 2.75rem" in _rule(".page-shell a:not(.button-link)", media="(max-width: 720px)")
    assert "display: block" in _rule(".schedule-cards__table,\n    .schedule-cards__table tbody,\n    .schedule-cards__table tr,\n    .schedule-cards__table th,\n    .schedule-cards__table td", media="(max-width: 720px)")
