"""Pure team readiness and position ordering rules."""

from collections.abc import Iterable


def season_ready_for_matchups(team_member_counts: Iterable[int]) -> bool:
    """Return true when the season has teams and every team has four members."""
    counts = tuple(team_member_counts)
    return bool(counts) and all(count == 4 for count in counts)


def order_members_by_seed(members: Iterable[tuple[int, int]]) -> list[int]:
    """Return golfer IDs by ascending (effective seed, golfer ID)."""
    return [golfer_id for golfer_id, _ in sorted(members, key=lambda item: (item[1], item[0]))]


__all__ = ["season_ready_for_matchups", "order_members_by_seed"]
