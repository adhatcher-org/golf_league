"""Player pages for a week's scores: the read-only week, the entry page and one match (R-SCORES).

Any verified user can see a week's scores. A player linked to a golfer who
plays that week can enter and change the scores of their own match, and of a
match they kept score for, until an admin locks it. The player is always
`Actor(user.id, False, user.golfer_id)`, also when the account is an admin:
admins use the admin pages. Rights come from the score service on the
server, never from a posted value. Every POST needs a CSRF token, responses
are `Cache-Control: no-store`, and nothing here logs a name or a score, or
shows a player who entered or changed a score.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from golf_league.database import get_session
from golf_league.domain.scorecard import Actor
from golf_league.models import (
    Course,
    HoleYardage,
    PlayerMatch,
    Season,
    TeeSet,
    User,
    Week,
)
from golf_league.routers.admin_week_scores import _day
from golf_league.routers.scorecard_flow import (
    PRIVATE_HEADERS,
    _csrf,
    _now,
    _page,
    _text,
    _today,
    purge_drafts,
)
from golf_league.security import require_verified_user
from golf_league.services.match_entry import parse_revision, parse_scores, score_field
from golf_league.services.scorecard_drafts import user_week_draft
from golf_league.services.scores import (
    EntryState,
    ScoreEntryError,
    SheetMatch,
    kept_matches,
    player_entry_state,
    player_entry_states,
    save_match_scores,
    week_for_entry,
    week_holes,
    week_points,
    week_sheet,
    writable_golfers,
)

logger = logging.getLogger(__name__)

router = APIRouter()

CLOSED = "Scores cannot be entered for this week."
LOCKED = "These scores are locked by the league admin. Ask the admin if something is wrong."
_LOCKED_MEANWHILE = "These scores were locked by the league admin. Nothing was saved."
_OUT_OF_DATE = ("These scores were changed after you opened this page. Nothing was saved. "
                "The saved scores are shown below; make your change again.")
_SAVE_AGAIN = "Scores were being changed at the same moment. Nothing was saved; save again."
_NOT_SAVED = "The scores could not be saved just now. Nothing was saved; save again."


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="Not found", headers=PRIVATE_HEADERS)


def _actor(user: User) -> Actor:
    return Actor(user.id, False, user.golfer_id)


def week_path(week_id: int) -> str:
    return f"/weeks/{week_id}/scores"


def enter_path(week_id: int) -> str:
    return f"/weeks/{week_id}/scores/enter"


def match_path(week_id: int, player_match_id: int) -> str:
    return f"/weeks/{week_id}/matches/{player_match_id}/scores"


def entry_link(week_id: int, state: EntryState) -> dict | None:
    """The player's score link for a week, by their own entry state; None when there is none."""
    if state.state == "open":
        return {"href": enter_path(week_id), "text": "Enter scores"}
    if state.state == "saved":
        return {"href": enter_path(week_id), "text": "Update scores"}
    if state.state == "locked":
        return {"href": week_path(week_id), "text": "View scores"}
    return None


def entry_links(session: Session, request: Request, week_ids: list[int], user: User) -> dict[int, dict]:
    """`entry_link` for each week that has one, keyed by week ID."""
    states = player_entry_states(session, week_ids, user.golfer_id, _today(request))
    links = {week_id: entry_link(week_id, state) for week_id, state in states.items()}
    return {week_id: link for week_id, link in links.items() if link is not None}


def opens_sentence(week: Week) -> str:
    return f"Scores for week {week.index} can be entered from {week.play_date.strftime('%b %-d, %Y')}."


def _week(session: Session, week_id: int) -> Week:
    week = session.get(Week, week_id)
    if week is None:
        raise _not_found()
    return week


def _sheet_match(session: Session, week_id: int, player_match_id: int) -> SheetMatch | None:
    return next((match for match in week_sheet(session, week_id) if match.player_match_id == player_match_id),
                None)


def _writable(session: Session, match_id: int, actor: Actor) -> frozenset[int]:
    match = session.get(PlayerMatch, match_id)
    return writable_golfers(session, match, actor) if match is not None else frozenset()


def _open_to(session: Session, week: Week, actor: Actor, today) -> bool:
    """Whether this player may write scores in the week at all today."""
    try:
        week_for_entry(session, week.id, today, actor)
    except ScoreEntryError:
        return False
    return True


def _title(match: SheetMatch) -> str:
    return f"{match.golfers[0].name} and {match.golfers[1].name}"


def _state_line(request: Request, match: SheetMatch) -> str:
    """A match's state in words, with no name of who entered, changed or locked it."""
    if match.locked:
        line = f"Locked by the league admin on {_day(request, match.locked_at)}."
    elif match.empty:
        line = "Not entered yet."
    else:
        line = "Entered."
    if not match.empty and match.scores_changed_at is not None:
        line += f" Last changed on {_day(request, match.scores_changed_at)}."
    return line


def _rows(match: SheetMatch, hole_numbers: list[int], input_ids: frozenset[int] | set[int],
          values: dict[str, str], invalid: set[str]) -> list[dict]:
    """A row per golfer: nine inputs for a golfer in `input_ids`, read-only otherwise."""
    rows = []
    for golfer in match.golfers:
        inputs = None
        if golfer.golfer_id in input_ids:
            inputs = [{"hole": hole, "field": score_field(golfer.golfer_id, hole),
                       "value": values.get(score_field(golfer.golfer_id, hole), ""),
                       "invalid": score_field(golfer.golfer_id, hole) in invalid} for hole in hole_numbers]
        rows.append({"golfer": golfer, "inputs": inputs})
    return rows


def _match_form(request: Request, session: Session, week: Week, match: SheetMatch, actor: Actor, *,
                values: dict[str, str] | None = None, errors: dict[str, str] | None = None,
                typed_on: int | None = None, editable: bool = True) -> dict:
    """The context of one match's form: the saved scores, or `values` as typed on revision `typed_on`.

    Typed values go back with the revision they were typed on, never a newer
    one, so saving them again cannot overwrite a change made in between.
    """
    hole_numbers = [hole.number for hole in week_holes(session, week.id)]
    errors = errors or {}
    writable = _writable(session, match.player_match_id, actor)
    editable = editable and not match.locked
    revision = match.revision
    if values is None:
        values = {score_field(golfer.golfer_id, hole): str(strokes)
                  for golfer in match.golfers for hole, strokes in golfer.strokes_by_hole.items()}
    else:
        revision = "" if typed_on is None else typed_on
    rows = _rows(match, hole_numbers, writable if editable else frozenset(), values, set(errors))
    return {
        "match": match, "title": _title(match), "state_line": _state_line(request, match),
        "view": {"rows": rows}, "hole_numbers": hole_numbers, "editable": editable, "revision": revision,
        "action": match_path(week.id, match.player_match_id),
        "field_ids": {cell["field"]: cell["field"] for row in rows for cell in row["inputs"] or []},
    }


def _group_matches(matches: list[dict], week_type: str) -> list[dict]:
    """Group match views for the scores page.

    Match weeks group by Team 1 vs Team 2 so the matchups between two teams sit
    together. Play-with-team weeks group by team so each team's players sit under
    the team. Groups keep the sheet's schedule order.
    """
    groups: dict[str, list[dict]] = {}
    for item in matches:
        match = item["match"]
        if week_type == "play_with_team":
            key = match.home_team_name  # home == away for a self-match
        else:
            key = f"{match.home_team_name} vs {match.away_team_name}"
        groups.setdefault(key, []).append(item)
    return [{"label": label, "matches": items} for label, items in groups.items()]


def _hole_details(session: Session, week: Week) -> dict:
    """Course and first tee set details for the played nine."""
    holes = week_holes(session, week.id)
    season = session.get(Season, week.season_id)
    course = session.get(Course, season.course_id)
    tee_set = session.scalars(
        select(TeeSet).where(TeeSet.course_id == season.course_id).order_by(TeeSet.sort_order).limit(1)
    ).one_or_none()
    yards_by_hole: dict[int, int | None] = {}
    if tee_set is not None and holes:
        for yardage in session.scalars(
            select(HoleYardage).where(
                HoleYardage.tee_set_id == tee_set.id,
                HoleYardage.hole_id.in_([hole.id for hole in holes]),
            )
        ):
            yards_by_hole[yardage.hole_id] = yardage.yards
    return {
        "holes": holes, "hole_numbers": [hole.number for hole in holes],
        "yards_by_hole": yards_by_hole,
        "course_name": course.name, "tee_label": tee_set.color_label if tee_set else None,
        "scoring_type": season.scoring_type,
    }


# --- the week's scores ---------------------------------------------------------


def _week_score_matches(
    session: Session,
    week: Week,
    actor: Actor,
    open_to_viewer: bool,
    is_admin: bool,
    sheet: list[SheetMatch],
    hole_numbers: list[int],
    points_by_match: dict,
    scoring_type: str,
) -> list[dict]:
    """Build the per-match score context for the player week scores page."""
    matches = []
    for match in sheet:
        link = None
        if is_admin and week.status == "scheduled" and week.week_type == "match":
            link = {
                "href": f"/admin/weeks/{week.id}/matches/{match.player_match_id}/scores",
                "text": "Unlock scores" if match.locked else (
                    "Enter scores" if match.empty else "Update scores"
                ),
            }
        writable = _writable(session, match.player_match_id, actor) if open_to_viewer else frozenset()
        if link is None and writable and not match.locked:
            entered = any(golfer.strokes_by_hole for golfer in match.golfers if golfer.golfer_id in writable)
            link = {"href": match_path(week.id, match.player_match_id),
                    "text": "Update scores" if entered else "Enter scores"}
        result = points_by_match.get(match.player_match_id)
        points_by_golfer = {r.golfer_id: r.points for r in result.rounds} if result is not None else {}
        round_cells = None
        round_bonus = None
        if result is not None:
            first_id, second_id = (golfer.golfer_id for golfer in match.golfers)
            if scoring_type == "match_play":
                round_cells = [
                    f"{hole.points_by_golfer[first_id]} | {hole.points_by_golfer[second_id]}"
                    for hole in result.holes
                ]
                bonus = {
                    round_.golfer_id: round_.points - sum(
                        hole.points_by_golfer[round_.golfer_id] for hole in result.holes
                    )
                    for round_ in result.rounds
                }
                round_bonus = f"{bonus[first_id]} | {bonus[second_id]}"
            else:
                lead = 0
                round_cells = []
                for hole in result.holes:
                    lead += hole.net_by_golfer[second_id] - hole.net_by_golfer[first_id]
                    round_cells.append(lead)
        slot = match.position_label
        matches.append({
            "match": match, "link": link, "points": points_by_golfer,
            "label": f"Player {'ABCD'[int(slot) - 1]}" if slot in ("1", "2", "3", "4") else f"Match {slot}",
            "round_cells": round_cells, "round_bonus": round_bonus,
            "view": {"rows": _rows(match, hole_numbers, frozenset(), {}, set())},
        })
    return matches


@router.get("/weeks/{week_id}/scores")
async def week_scores(week_id: int, request: Request,
                      session: Session = Depends(get_session),  # noqa: B008
                      user: User = Depends(require_verified_user)) -> Response:  # noqa: B008
    week = _week(session, week_id)
    actor, today = _actor(user), _today(request)
    team = request.query_params.get("team")
    # The week dropdown at the top of the page GETs ?week=<id>; follow it to that
    # week's scores page so the URL stays the page the user actually asked for.
    target_week = request.query_params.get("week")
    if target_week is not None:
        try:
            target_id = int(target_week)
        except ValueError:
            target_id = None
        if target_id is not None and target_id != week_id:
            target = session.get(Week, target_id)
            if target is not None and target.season_id == week.season_id:
                return RedirectResponse(f"/weeks/{target_id}/scores", status_code=303,
                                        headers=PRIVATE_HEADERS)
    sheet = week_sheet(session, week.id)
    hole_details = _hole_details(session, week)
    hole_numbers = hole_details["hole_numbers"]
    open_to_viewer = _open_to(session, week, actor, today)
    # Weeks of this season for the week switcher; teams present this week for the filter.
    weeks = list(session.scalars(
        select(Week).where(Week.season_id == week.season_id).order_by(Week.index)
    ))
    teams = sorted({match.home_team_name for match in sheet} | {match.away_team_name for match in sheet})
    if team:
        sheet = [match for match in sheet
                 if match.home_team_name == team or match.away_team_name == team]
    points_by_match = week_points(session, week.id)
    matches = _week_score_matches(
        session, week, actor, open_to_viewer, user.is_admin, sheet, hole_numbers, points_by_match,
        hole_details["scoring_type"],
    )
    groups = _group_matches(matches, week.week_type)
    return _page(request, "player/scores/week.html", {
        "week": week, "matches": matches, "groups": groups, **hole_details,
        "weeks": weeks, "teams": teams, "team": team,
        "saved": request.query_params.get("saved") == "1", "errors": {},
    })


# --- the entry page --------------------------------------------------------------


def entry_page(request: Request, session: Session, week: Week, user: User, *,
               errors: dict[str, str] | None = None, status_code: int = 200) -> Response:
    """The entry page for the player's own match, by their entry state; 404 when they do not play.

    Where the player can enter scores at all, it also offers a scorecard:
    upload a photo or type a card (GL-86), even when their own match is saved
    or locked, since they may be keeping score for the other pair.
    """
    actor, today = _actor(user), _today(request)
    purge_drafts(request, session)
    state = player_entry_state(session, week.id, user.golfer_id, today)
    if state.state == "not_playing":
        raise _not_found()
    context: dict = {"week": week, "state": state.state, "errors": errors or {}, "field_ids": {"photo": "photo"},
                     "form": None, "kept": [], "message": "", "scores_path": week_path(week.id), "card": None,
                     "discarded": request.query_params.get("discarded") == "1" and status_code == 200}
    if state.state == "closed":
        context["message"] = CLOSED
    elif state.state == "not_open_yet":
        context["message"] = opens_sentence(week)
    else:
        match = _sheet_match(session, week.id, state.player_match_id)
        if match is None:  # the match went between the two reads
            raise _not_found()
        context["form"] = _match_form(request, session, week, match, actor)
        if state.state == "locked":
            context["message"] = LOCKED
        else:
            sheet = {item.player_match_id: item for item in week_sheet(session, week.id)}
            context["kept"] = [{"href": match_path(week.id, match_id), "title": _title(sheet[match_id])}
                               for match_id in kept_matches(session, week.id, actor) if match_id in sheet]
        draft = user_week_draft(session, user.id, week.id, _now(request))
        context["card"] = {"action": f"/weeks/{week.id}/scorecards",
                           "configured": request.app.state.vision_provider is not None,
                           "in_progress": f"/scorecards/{draft.id}" if draft is not None else None}
    return _page(request, "player/scores/enter.html", context, status_code)


@router.get("/weeks/{week_id}/scores/enter")
async def enter_scores(week_id: int, request: Request,
                       session: Session = Depends(get_session),  # noqa: B008
                       user: User = Depends(require_verified_user)) -> Response:  # noqa: B008
    return entry_page(request, session, _week(session, week_id), user)


# --- one match --------------------------------------------------------------------


def _match_page(request: Request, session: Session, week: Week, player_match_id: int, actor: Actor, *,
                values: dict[str, str] | None = None, errors: dict[str, str] | None = None,
                typed_on: int | None = None, message: str = "", status_code: int = 200) -> Response:
    match = _sheet_match(session, week.id, player_match_id)
    if match is None or not _writable(session, player_match_id, actor):
        raise _not_found()
    today = _today(request)
    notice = ""
    if match.locked:
        notice = LOCKED
    elif week.play_date > today and week.status == "scheduled":
        notice = opens_sentence(week)
    elif not _open_to(session, week, actor, today):
        notice = CLOSED
    form = _match_form(request, session, week, match, actor, values=values, errors=errors, typed_on=typed_on,
                       editable=not notice)
    message = message or notice
    return _page(request, "player/scores/match.html", {
        "week": week, "form": form, "errors": errors or {}, "field_ids": form["field_ids"], "message": message,
        **_hole_details(session, week),
        "scores_path": week_path(week.id), "enter_path": enter_path(week.id),
    }, status_code)


@router.get("/weeks/{week_id}/matches/{player_match_id}/scores")
async def match_scores(week_id: int, player_match_id: int, request: Request,
                       session: Session = Depends(get_session),  # noqa: B008
                       user: User = Depends(require_verified_user)) -> Response:  # noqa: B008
    return _match_page(request, session, _week(session, week_id), player_match_id, _actor(user))


def _refused(request: Request, session: Session, week_id: int, player_match_id: int, actor: Actor,
             values: dict[str, str], exc: ScoreEntryError, typed_on: int | None) -> Response:
    """The match page after the score service refused a save; nothing was written."""
    if exc.code in ("not_found", "not_allowed"):
        raise _not_found() from exc
    week = _week(session, week_id)
    if exc.code == "locked":
        return _match_page(request, session, week, player_match_id, actor, status_code=409,
                           message=_LOCKED_MEANWHILE)
    if exc.code == "out_of_date":
        return _match_page(request, session, week, player_match_id, actor, status_code=409,
                           errors={"match": _OUT_OF_DATE})
    if exc.code == "conflict":
        return _match_page(request, session, week, player_match_id, actor, values=values, typed_on=typed_on,
                           status_code=409, errors={"match": _SAVE_AGAIN})
    if exc.code == "not_open_yet":
        return _match_page(request, session, week, player_match_id, actor, status_code=422,
                           message=opens_sentence(week))
    if exc.code in ("cancelled", "played", "no_matchup"):
        return _match_page(request, session, week, player_match_id, actor, status_code=422, message=CLOSED)
    return _match_page(request, session, week, player_match_id, actor, values=values, typed_on=typed_on,
                       status_code=422, errors={"match": exc.message})


@router.post("/weeks/{week_id}/matches/{player_match_id}/scores")
async def match_scores_save(week_id: int, player_match_id: int, request: Request,
                            session: Session = Depends(get_session),  # noqa: B008
                            user: User = Depends(require_verified_user)) -> Response:  # noqa: B008
    form = await request.form()
    _csrf(request, form.get("csrf_token", ""))
    week = _week(session, week_id)
    actor, today, now = _actor(user), _today(request), _now(request)
    match = _sheet_match(session, week_id, player_match_id)
    writable = _writable(session, player_match_id, actor) if match is not None else frozenset()
    if not writable:
        raise _not_found()
    hole_numbers = [hole.number for hole in week_holes(session, week_id)]
    # Only the golfers this player may write are read; a field for anyone else is ignored.
    golfers = [(golfer.golfer_id, golfer.name) for golfer in match.golfers if golfer.golfer_id in writable]
    values = {score_field(golfer_id, hole): _text(form, score_field(golfer_id, hole))
              for golfer_id, _ in golfers for hole in hole_numbers}
    scores, errors = parse_scores(values, golfers, hole_numbers)
    base_revision = parse_revision(_text(form, "revision"))
    if not match.locked and week.status == "scheduled" and week.play_date > today:
        return _refused(request, session, week_id, player_match_id, actor, values,
                        ScoreEntryError("not_open_yet", ""), base_revision)
    # A form with no readable revision, or with mistakes typed on an older page, is out of date
    # before anything else: it goes back with the saved values, never with what was typed.
    if base_revision is None or (errors and not match.locked and base_revision != match.revision):
        return _refused(request, session, week_id, player_match_id, actor, values,
                        ScoreEntryError("out_of_date", ""), base_revision)
    if errors and not match.locked and _open_to(session, week, actor, today):
        return _match_page(request, session, week, player_match_id, actor, values=values, errors=errors,
                           typed_on=base_revision, status_code=422)
    session.rollback()  # end the read transaction: the write reserves the single writer first
    try:
        save_match_scores(session, week_id, player_match_id, scores, actor, today,
                          base_revision=base_revision, now=now)
        session.commit()
    except ScoreEntryError as exc:
        session.rollback()
        return _refused(request, session, week_id, player_match_id, actor, values, exc, base_revision)
    except (SQLAlchemyError, OSError) as exc:
        session.rollback()
        logger.warning("player match save failed week=%d match=%d type=%s", week_id, player_match_id,
                       type(exc).__name__)
        return _match_page(request, session, _week(session, week_id), player_match_id, actor, values=values,
                           typed_on=base_revision, status_code=503, errors={"match": _NOT_SAVED})
    logger.info("player match saved week=%d match=%d user=%d", week_id, player_match_id, actor.user_id)
    return RedirectResponse(f"{week_path(week_id)}?saved=1#match-{player_match_id}", status_code=303,
                            headers=PRIVATE_HEADERS)
