"""Pure scoring rules: net scores, Match Play points and Stroke Play points.

No I/O, no database, no HTTP. Every function takes plain values and returns
plain values so a route or service can use it without import-layer violations
(enforced by ``tests/test_architecture.py``).
"""

from dataclasses import dataclass

VALID_SCORING_TYPES = ("match_play", "stroke_play")
DEFAULT_SCORING_TYPE = "match_play"


@dataclass(frozen=True)
class GolferCard:
    """One golfer's gross scores for the holes being scored.

    `strokes_by_hole` maps hole number to gross strokes.
    """

    golfer_id: int
    name: str
    handicap: int | None
    strokes_by_hole: dict[int, int]


@dataclass(frozen=True)
class HoleResult:
    """One hole's net scores and Match Play points for both golfers."""

    hole_number: int
    stroke_index: int
    net_by_golfer: dict[int, int]
    points_by_golfer: dict[int, int]  # Match Play points only; zero for stroke play


@dataclass(frozen=True)
class RoundResult:
    """One golfer's round: gross and net totals, and points."""

    golfer_id: int
    name: str
    gross_total: int
    net_total: int
    points: int


@dataclass(frozen=True)
class MatchResult:
    """One match's complete scoring result under the season's scoring type."""

    scoring_type: str
    holes: list[HoleResult]
    rounds: list[RoundResult]


def net_score_for_hole(gross: int, handicap: int | None, stroke_index: int) -> int:
    """Net score for one hole.

    A handicap of h grants one stroke on each of the h holes with the lowest
    stroke index. So a handicap of 4 subtracts 1 from gross on holes whose
    stroke index is 1, 2, 3 or 4. A null or non-positive handicap grants
    nothing, and net equals gross on every remaining hole.
    """
    if handicap is None or handicap <= 0:
        return gross
    if stroke_index <= handicap:
        return gross - 1
    return gross


def match_play_hole_points(net_a: int, net_b: int) -> tuple[int, int]:
    """Match Play points for one hole: (golfer_a_points, golfer_b_points).

    The lower net score wins 2 points. A tie awards 1 point to each.
    """
    if net_a < net_b:
        return (2, 0)
    if net_b < net_a:
        return (0, 2)
    return (1, 1)


def stroke_play_points(net_total_a: int, net_total_b: int) -> tuple[int, int]:
    """Stroke Play points: (golfer_a_points, golfer_b_points).

    The lower net total wins the difference between the two net totals. A tie
    awards 0 points to each.
    """
    if net_total_a < net_total_b:
        return (net_total_b - net_total_a, 0)
    if net_total_b < net_total_a:
        return (0, net_total_a - net_total_b)
    return (0, 0)


def _net_by_hole(
    stroke_index_by_hole: dict[int, int],
    golfer_a: GolferCard,
    golfer_b: GolferCard,
) -> dict[int, dict[int, int]]:
    """Net score per hole per golfer, keyed by hole number then golfer id."""
    golfers = (golfer_a, golfer_b)
    hole_numbers = sorted(stroke_index_by_hole)
    result: dict[int, dict[int, int]] = {}
    for hole in hole_numbers:
        si = stroke_index_by_hole[hole]
        result[hole] = {
            golfer.golfer_id: net_score_for_hole(
                golfer.strokes_by_hole.get(hole, 0), golfer.handicap, si
            )
            for golfer in golfers
        }
    return result


def _hole_points(
    scoring_type: str, net_a: int, net_b: int
) -> tuple[int, int]:
    """Points awarded on one hole under the scoring type (zero for stroke play)."""
    if scoring_type == "match_play":
        return match_play_hole_points(net_a, net_b)
    return (0, 0)


def _round_data(
    golfer_a: GolferCard,
    golfer_b: GolferCard,
    hole_numbers: list[int],
    net_by_hole: dict[int, dict[int, int]],
    holes: list[HoleResult],
) -> list[dict[str, object]]:
    """Per-golfer round totals: gross, net, hole points, and bonus points."""
    data: list[dict[str, object]] = []
    for golfer in (golfer_a, golfer_b):
        gross_total = sum(golfer.strokes_by_hole.get(hole, 0) for hole in hole_numbers)
        net_total = sum(net_by_hole[hole][golfer.golfer_id] for hole in hole_numbers)
        hole_points = sum(
            holes[i].points_by_golfer[golfer.golfer_id] for i in range(len(holes))
        )
        data.append(
            {
                "golfer": golfer,
                "gross_total": gross_total,
                "net_total": net_total,
                "hole_points": hole_points,
                "bonus_points": 0,
            }
        )
    return data


def _apply_match_play_bonus(round_data: list[dict[str, object]]) -> None:
    """Award 2 bonus points to each golfer tied for the lowest net total."""
    lowest_net = min(rd["net_total"] for rd in round_data)
    for rd in round_data:
        if rd["net_total"] == lowest_net:
            rd["bonus_points"] = 2


def _stroke_play_round_points(rounds: list[RoundResult]) -> list[RoundResult]:
    """Replace round points with the stroke-play difference between the two rounds."""
    ra, rb = rounds[0], rounds[1]
    pts_a, pts_b = stroke_play_points(ra.net_total, rb.net_total)
    return [
        RoundResult(
            golfer_id=ra.golfer_id,
            name=ra.name,
            gross_total=ra.gross_total,
            net_total=ra.net_total,
            points=pts_a,
        ),
        RoundResult(
            golfer_id=rb.golfer_id,
            name=rb.name,
            gross_total=rb.gross_total,
            net_total=rb.net_total,
            points=pts_b,
        ),
    ]


def score_match(
    scoring_type: str,
    stroke_index_by_hole: dict[int, int],
    golfer_a: GolferCard,
    golfer_b: GolferCard,
) -> MatchResult:
    """Score one two-golfer match under the season's scoring type.

    `stroke_index_by_hole` maps hole number to its stroke index within the
    nine being played. Each golfer's card holds gross strokes by hole number;
    missing holes count as 0 gross.
    """
    if scoring_type not in VALID_SCORING_TYPES:
        raise ValueError(f"Unknown scoring type: {scoring_type!r}")

    hole_numbers = sorted(stroke_index_by_hole)
    net_by_hole = _net_by_hole(stroke_index_by_hole, golfer_a, golfer_b)

    holes: list[HoleResult] = []
    for hole in hole_numbers:
        nets = net_by_hole[hole]
        net_a, net_b = nets[golfer_a.golfer_id], nets[golfer_b.golfer_id]
        pts_a, pts_b = _hole_points(scoring_type, net_a, net_b)
        holes.append(
            HoleResult(
                hole_number=hole,
                stroke_index=stroke_index_by_hole[hole],
                net_by_golfer=dict(nets),
                points_by_golfer={golfer_a.golfer_id: pts_a, golfer_b.golfer_id: pts_b},
            )
        )

    round_data = _round_data(golfer_a, golfer_b, hole_numbers, net_by_hole, holes)
    if scoring_type == "match_play":
        _apply_match_play_bonus(round_data)

    rounds = [
        RoundResult(
            golfer_id=rd["golfer"].golfer_id,
            name=rd["golfer"].name,
            gross_total=rd["gross_total"],
            net_total=rd["net_total"],
            points=int(rd["hole_points"]) + int(rd["bonus_points"]),
        )
        for rd in round_data
    ]
    if scoring_type == "stroke_play":
        rounds = _stroke_play_round_points(rounds)

    return MatchResult(scoring_type=scoring_type, holes=holes, rounds=rounds)


# --- R-RESULTS@1: signed nine-hole snapshot scoring -------------------------


def strokes_for_relative(relative: int, stroke_index_by_hole: dict[int, int]) -> dict[int, int]:
    """Allocate nonnegative ``relative`` strokes over the played nine.

    Uses ``divmod(relative, 9)``: for ``9q + r`` strokes, give ``q`` strokes on
    every hole and one more on stroke indexes 1 through ``r``. So 10 gives two
    strokes on index 1 and one on indexes 2-9; 12 gives two on indexes 1-3 and
    one on indexes 4-9. Multiple strokes per hole are allowed (nothing is
    capped at one).
    """
    q, r = divmod(relative, 9)
    return {hole: q + (1 if index <= r else 0)
            for hole, index in stroke_index_by_hole.items()}


@dataclass(frozen=True)
class ResultHole:
    """One hole's gross strokes, allocated strokes, net scores and points."""

    hole_number: int
    stroke_index: int
    gross_by_golfer: dict[int, int]
    strokes_by_golfer: dict[int, int]
    net_by_golfer: dict[int, int]
    points_by_golfer: dict[int, int]


@dataclass(frozen=True)
class ResultRound:
    """One golfer's round under R-RESULTS@1: gross, net and points."""

    golfer_id: int
    name: str
    gross_total: int
    net_total: int
    points: int


@dataclass(frozen=True)
class ResultMatch:
    """One match's complete R-RESULTS@1 result."""

    holes: list[ResultHole]
    rounds: list[ResultRound]


def score_result_match(
    scoring_type: str,
    stroke_index_by_hole: dict[int, int],
    golfer_a: GolferCard,
    golfer_b: GolferCard,
) -> ResultMatch | None:
    """Score one two-golfer match under R-RESULTS@1.

    Returns ``None`` when either handicap is missing (the match is pending;
    a missing snapshot is never replaced with zero).

    Uses relative signed strokes between the opponents: subtract the lower
    handicap from both, then allocate each nonnegative difference over the
    played nine by ``stroke_index_9``. Per-hole net is gross minus the
    allocated strokes. The net round total is gross minus the *signed*
    handicap, so a negative handicap adds its absolute value (gross 42 at -2
    is net 44). Match-play base points apply per hole (2 for the lower net,
    1 each on a tie) plus 2 bonus points to each golfer tied for the lowest
    net total; stroke play uses the net-round-total difference.
    """
    if scoring_type not in VALID_SCORING_TYPES:
        raise ValueError(f"Unknown scoring type: {scoring_type!r}")

    hc_a, hc_b = golfer_a.handicap, golfer_b.handicap
    if hc_a is None or hc_b is None:
        return None

    hole_numbers = sorted(stroke_index_by_hole)
    lower = min(hc_a, hc_b)
    a_strokes = strokes_for_relative(hc_a - lower, stroke_index_by_hole)
    b_strokes = strokes_for_relative(hc_b - lower, stroke_index_by_hole)

    holes: list[ResultHole] = []
    for hole in hole_numbers:
        si = stroke_index_by_hole[hole]
        gross_a = golfer_a.strokes_by_hole.get(hole, 0)
        gross_b = golfer_b.strokes_by_hole.get(hole, 0)
        net_a = gross_a - a_strokes[hole]
        net_b = gross_b - b_strokes[hole]
        if scoring_type == "match_play":
            if net_a < net_b:
                pts = {golfer_a.golfer_id: 2, golfer_b.golfer_id: 0}
            elif net_b < net_a:
                pts = {golfer_a.golfer_id: 0, golfer_b.golfer_id: 2}
            else:
                pts = {golfer_a.golfer_id: 1, golfer_b.golfer_id: 1}
        else:
            pts = {golfer_a.golfer_id: 0, golfer_b.golfer_id: 0}
        holes.append(ResultHole(
            hole_number=hole, stroke_index=si,
            gross_by_golfer={golfer_a.golfer_id: gross_a, golfer_b.golfer_id: gross_b},
            strokes_by_golfer={golfer_a.golfer_id: a_strokes[hole],
                               golfer_b.golfer_id: b_strokes[hole]},
            net_by_golfer={golfer_a.golfer_id: net_a, golfer_b.golfer_id: net_b},
            points_by_golfer=pts,
        ))

    gross_total_a = sum(golfer_a.strokes_by_hole.get(hole, 0) for hole in hole_numbers)
    gross_total_b = sum(golfer_b.strokes_by_hole.get(hole, 0) for hole in hole_numbers)
    net_total_a = gross_total_a - hc_a
    net_total_b = gross_total_b - hc_b

    hole_points_a = sum(h.points_by_golfer[golfer_a.golfer_id] for h in holes)
    hole_points_b = sum(h.points_by_golfer[golfer_b.golfer_id] for h in holes)

    if scoring_type == "match_play":
        lowest_net = min(net_total_a, net_total_b)
        rounds = [
            ResultRound(golfer_a.golfer_id, golfer_a.name, gross_total_a, net_total_a,
                        hole_points_a + (2 if net_total_a == lowest_net else 0)),
            ResultRound(golfer_b.golfer_id, golfer_b.name, gross_total_b, net_total_b,
                        hole_points_b + (2 if net_total_b == lowest_net else 0)),
        ]
    else:
        if net_total_a < net_total_b:
            pts_a, pts_b = net_total_b - net_total_a, 0
        elif net_total_b < net_total_a:
            pts_a, pts_b = 0, net_total_a - net_total_b
        else:
            pts_a = pts_b = 0
        rounds = [
            ResultRound(golfer_a.golfer_id, golfer_a.name, gross_total_a, net_total_a, pts_a),
            ResultRound(golfer_b.golfer_id, golfer_b.name, gross_total_b, net_total_b, pts_b),
        ]

    return ResultMatch(holes=holes, rounds=rounds)
