# 2. Handicap Representation

## Status

Accepted for the MVP.

## Context

The league plays nine-hole matches from two tees. A golfer's playing handicap
must remain meaningful at the tee where it was entered, including for plus
handicaps, while a future release may need a separate handicap index.

## Decision

- `golfers.handicap_strokes` and `season_participants.seed_handicap_strokes`
  are nullable signed integer stroke counts. `NULL` means no handicap is on
  file, `0` means scratch, and a negative count is a valid plus handicap.
- The stored number is a nine-hole playing handicap used as entered: it is
  never halved, converted between tees, or derived from an index.
- `golfers.handicap_index` remains a nullable numeric field reserved for a
  later release. The MVP does not write it.
- The pure `course_handicap(index, rating, slope, par)` helper calculates
  `index * (slope / 113) + (rating - par)` with `Decimal` values and rounds
  once using `floor(value + Decimal("0.5"))`.

| Exact value | Returned strokes |
| --- | ---: |
| 2.4 | 2 |
| 2.5 | 3 |
| 3.5 | 4 |
| -2.5 | -2 |
| -3.5 | -3 |

This is nearest-whole-number rounding with ties toward positive infinity. It
is deliberately neither Python's banker's `round()` nor Decimal
`ROUND_HALF_UP`, which would send negative ties away from zero.

The helper has no live integration in the MVP: importers, season seeds, week
snapshots, routes, templates, recompute jobs, and index derivation do not call
it. A static import guard preserves that isolation.

## Consequences

The two league tee values cannot be derived from one another. Their real gaps
are one or two strokes and do not scale with handicap, so a tee change needs a
separate entered playing handicap. Future index-based calculations can use the
isolated helper without retroactively changing stored playing handicaps or
snapshots.
