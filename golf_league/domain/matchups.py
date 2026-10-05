"""Pure slot shapes and team participation rules for explicit pairings."""

from collections.abc import Sequence


def pairing_shape(home_team_id: int, away_team_id: int) -> list[tuple[str, int, int, bool]]:
    if home_team_id == away_team_id:
        return [("P1vP2", 1, 2, True), ("P3vP4", 3, 4, True)]
    return [(str(position), position, position, False) for position in range(1, 5)]


def validate_team_once_per_week(home_team_id: int, away_team_id: int,
                                existing_pairings: Sequence[tuple[int, int]]) -> None:
    candidates = {home_team_id, away_team_id}
    if any(candidates.intersection(pair) for pair in existing_pairings):
        raise ValueError("Each team can appear in only one pairing per week.")
