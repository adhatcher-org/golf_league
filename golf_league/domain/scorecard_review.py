"""Pure scorecard review rules: matching a written name, and checking a card.

Players write first names only on a card, usually with their handicap beside
the name. Nothing here trusts what a model read: a name only ever suggests one
of the week's own golfers, and the checks are warnings for the admin, never a
reason to accept or refuse a card.

No SQLAlchemy, FastAPI or `golf_league.models` import (`tests/test_architecture.py`).
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher

FUZZY_RATIO = 0.8

_DROPPED = re.compile(r"['’`.,]")
_SEPARATORS = re.compile(r"[^\w\s]|_")  # punctuation; letters of any alphabet and digits stay


@dataclass(frozen=True)
class NameCandidate:
    """One golfer in the week's matchups; `handicap` is that week's stored strokes."""

    golfer_id: int
    first_name: str
    last_name: str
    handicap: int | None = None


@dataclass(frozen=True)
class NameMatch:
    """The golfer to preselect, if any. `flag` is "", "check" or "handicap"."""

    golfer_id: int | None
    flag: str = ""


NO_MATCH = NameMatch(None)


def normalise_name(text: object) -> str:
    """Lower-case words and single spaces: case, spacing and punctuation are ignored.

    A word that is only digits is dropped: it is the handicap written beside
    the name (`Aaron 8`), not part of the name.
    """
    lowered = _DROPPED.sub("", str(text or "").casefold())
    words = _SEPARATORS.sub(" ", lowered).split()
    return " ".join(word for word in words if not word.isdecimal())


def _first(candidate: NameCandidate) -> str:
    return normalise_name(candidate.first_name)


def _full(candidate: NameCandidate) -> str:
    return normalise_name(f"{candidate.first_name} {candidate.last_name}")


def _first_and_start_of_last(written: str, candidate: NameCandidate) -> bool:
    """`aaron h` or `aaron ha` for Aaron Hatcher: the first name, then an initial or more."""
    first, last = _first(candidate), normalise_name(candidate.last_name)
    rest = written[len(first):].strip() if written.startswith(first + " ") else ""
    return bool(first and rest) and last.startswith(rest)


def _closeness(written: str, candidate: NameCandidate) -> float:
    ratio = SequenceMatcher(None, written, _first(candidate)).ratio()
    if " " in written:
        ratio = max(ratio, SequenceMatcher(None, written, _full(candidate)).ratio())
    return ratio


def match_name(written: object, candidates: Iterable[NameCandidate], *,
               handicap_written: int | None = None) -> NameMatch:
    """Suggest which of the week's golfers a written name is.

    A single golfer whose name matches exactly (full name, first name plus the
    start of the last name, or first name alone) is preselected. Several with
    the same first name leave the choice to the admin, unless the handwritten
    handicap equals exactly one of their stored handicaps. Failing an exact
    match, a single golfer whose name is at least 0.8 similar is preselected
    and flagged to be checked. Anything else selects nobody.
    """
    name = normalise_name(written)
    if not name:
        return NO_MATCH
    golfers = list({candidate.golfer_id: candidate for candidate in candidates}.values())
    exact_groups = (
        [golfer for golfer in golfers if name == _full(golfer)],
        [golfer for golfer in golfers if _first_and_start_of_last(name, golfer)],
        [golfer for golfer in golfers if name == _first(golfer)],
    )
    for group in exact_groups:
        if len(group) == 1:
            return NameMatch(group[0].golfer_id)
        if len(group) > 1:
            if handicap_written is not None:
                same = [golfer for golfer in group if golfer.handicap == handicap_written]
                if len(same) == 1:
                    return NameMatch(same[0].golfer_id, "handicap")
            return NO_MATCH
    close = [golfer for golfer in golfers if _closeness(name, golfer) >= FUZZY_RATIO]
    if len(close) == 1:
        return NameMatch(close[0].golfer_id, "check")
    return NO_MATCH


def card_side(holes_header: Sequence[int]) -> str | None:
    """`front` for holes 1 to 9, `back` for 10 to 18, None when the header does not say."""
    if not holes_header:
        return None
    if all(1 <= number <= 9 for number in holes_header):
        return "front"
    if all(10 <= number <= 18 for number in holes_header):
        return "back"
    return None


@dataclass(frozen=True)
class OutMismatch:
    """The handwritten Out total differs from the sum of the scores shown."""

    shown_sum: int
    out_total: int
    may_be_net: bool


def out_total_mismatch(scores: Sequence[int | None], out_total: int | None,
                       handicap_written: int | None) -> OutMismatch | None:
    """Compare a complete row with its handwritten total; None when they agree or cannot be compared.

    A total below the sum, beside a written handicap, is flagged as possibly the
    net score: the card's last column is the net, and a model can read it as Out.
    """
    if out_total is None or not scores or any(score is None for score in scores):
        return None
    shown_sum = sum(scores)  # type: ignore[arg-type]
    if shown_sum == out_total:
        return None
    may_be_net = handicap_written is not None and (
        out_total == shown_sum - handicap_written or out_total < shown_sum
    )
    return OutMismatch(shown_sum, out_total, may_be_net)
