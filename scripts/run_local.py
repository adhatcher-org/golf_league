"""Run the app locally for manual browser journeys, with synthetic data only.

    uv run python scripts/run_local.py --data-dir /tmp/golf-league-local [--demo] [--port 8000]

Safety rails (the script refuses to start otherwise):
- everything lives in --data-dir, which must not be the repository's ./data;
  the database, the managed settings file and captured mail are all inside it;
- the public origin is http://127.0.0.1:<port>, so cookies are plain-http
  cookies a local browser accepts (no Secure flag);
- SMTP_HOST must not be set: mail is never sent. Each message is written to
  <data-dir>/mail/ (mode 0600) so a tester can open verification, reset and
  set-password links. Those tokens are local, synthetic and short-lived.

The first admin is bootstrapped from ADMIN_EMAIL/ADMIN_PASSWORD when set,
otherwise from the synthetic defaults printed at start-up. --demo seeds a
synthetic Fall-shaped season (8 teams x 4, 10 printed weeks, two paired
match weeks, one substitution) and four player personas.
"""

import argparse
import os
import re
import secrets
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO_PASSWORD = "local-demo-password"
DEFAULT_ADMIN_EMAIL = "admin@example.test"
DEFAULT_ADMIN_PASSWORD = "local-admin-password"
PERSONAS = {
    "regular": "player-regular@example.test",
    "substitute": "player-sub@example.test",
    "unused": "player-unused@example.test",
    "unlinked": "player-unlinked@example.test",
}


class FileMailSender:
    """Record each message as a private file instead of sending it."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.sent: list[dict[str, str]] = []

    def send(self, to: str, subject: str, body: str) -> None:
        self.sent.append({"to": to, "subject": subject, "body": body})
        slug = re.sub(r"[^a-z0-9]+", "-", subject.lower()).strip("-")[:40]
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
        path = self.directory / f"{stamp}-{slug}.txt"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"To: {to}\nSubject: {subject}\n\n{body}\n")


def prepare_environment(data_dir: Path, port: int) -> dict[str, str]:
    """Validate the rails and return the environment the app will read."""
    data_dir = data_dir.resolve()
    if data_dir == ROOT.resolve() or ROOT.resolve() in data_dir.parents:
        raise SystemExit("refusing a --data-dir inside the repository; pass a scratch directory outside it")
    if os.environ.get("SMTP_HOST"):
        raise SystemExit("refusing to run with SMTP_HOST set: local runs never send mail")
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    managed = data_dir / "managed.env"
    if not managed.exists():
        fd = os.open(managed, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f'SESSION_SECRET="{secrets.token_urlsafe(32)}"\n')
    return {
        "DATABASE_URL": f"sqlite:///{data_dir / 'golf_league.db'}",
        "MANAGED_ENV_PATH": str(managed),
        "EXTERNAL_BASE_URL": f"http://127.0.0.1:{port}",
        "ADMIN_EMAIL": os.environ.get("ADMIN_EMAIL") or DEFAULT_ADMIN_EMAIL,
        "ADMIN_PASSWORD": os.environ.get("ADMIN_PASSWORD") or DEFAULT_ADMIN_PASSWORD,
    }


def _demo_weeks(season_id: int) -> list:
    """Ten printed weeks shaped like the Fall sheet: 1 cancelled team week, 7 match
    weeks, 1 play-with-your-team week, 1 reserved rain date; nines alternate."""
    from golf_league.models import Week

    weeks, nine = [], "front"
    for index in range(1, 11):
        play_date = date(2026, 8, 27) + timedelta(days=7 * (index - 1))
        if index == 10:
            weeks.append(Week(season_id=season_id, index=index, play_date=play_date, nine=None,
                              week_type="rain_date", status="scheduled"))
            continue
        week_type = "play_with_team" if index in (1, 9) else "match"
        weeks.append(Week(season_id=season_id, index=index, play_date=play_date, nine=nine,
                          week_type=week_type, status="cancelled" if index == 1 else "scheduled"))
        nine = "back" if nine == "front" else "front"
    return weeks


def seed_demo(database_url: str) -> bool:
    """Seed the synthetic demo season once; return False when data already exists."""
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from golf_league.admin_config import bootstrap_admin
    from golf_league.database import make_engine
    from golf_league.migrations import upgrade_to_head
    from golf_league.models import (
        Golfer,
        PlayerMatch,
        Season,
        SeasonGolfer,
        Team,
        TeamMember,
        TeeSet,
        User,
    )
    from golf_league.services.auth import hash_password
    from golf_league.services.course_seed import seed_wyandot
    from golf_league.services.matchups import (
        create_team_match,
        generate_player_matches,
        substitute_player,
    )

    def clock():
        return datetime.now(UTC)

    upgrade_to_head(database_url)
    engine = make_engine(database_url)
    try:
        with Session(engine) as session:
            if session.scalar(select(Season.id).limit(1)) is not None:
                return False
            # Personas are users too, so create the first admin before them.
            bootstrap_admin(session, managed_env_path=os.environ.get("MANAGED_ENV_PATH"))
            seed_wyandot(session)
            tees = {tee.name: tee for tee in session.scalars(select(TeeSet))}
            season = Season(year=2026, course_id=tees["Deer"].course_id, start_date=date(2026, 8, 27),
                            end_date=date(2026, 10, 29), play_weekday=3, status="active",
                            first_week_nine="front")
            session.add(season)
            session.flush()

            teams, team_golfers = [], []
            for number in range(1, 9):
                team = Team(season_id=season.id, name=f"Team {number}", number=number, sort_order=number)
                session.add(team)
                session.flush()
                teams.append(team.id)
                for position in range(1, 5):
                    tee = tees["Deer"] if position % 2 else tees["Snake"]
                    golfer = Golfer(first_name=f"Demo{number}{position}", last_name="Synthetic",
                                    email=f"demo.t{number}p{position}@example.test", phone=None,
                                    default_tee_set_id=tee.id, handicap_strokes=position * 2 - 3,
                                    handicap_source="self_reported", handicap_status="ok")
                    session.add(golfer)
                    session.flush()
                    team_golfers.append(golfer.id)
                    session.add(SeasonGolfer(season_id=season.id, golfer_id=golfer.id))
                    session.add(TeamMember(team_id=team.id, season_id=season.id, golfer_id=golfer.id,
                                           position=position))
            subs = []
            for number in range(1, 5):
                golfer = Golfer(first_name=f"Sub{number}", last_name="Synthetic-With-A-Much-Longer-Surname",
                                email=f"demo.sub{number}@example.test", phone=None,
                                default_tee_set_id=tees["Deer"].id,
                                # The last sub has no handicap: an empty-state example.
                                handicap_strokes=None if number == 4 else number + 6,
                                handicap_source="self_reported",
                                handicap_status="needs_entry" if number == 4 else "ok")
                session.add(golfer)
                session.flush()
                subs.append(golfer.id)

            weeks = _demo_weeks(season.id)
            session.add_all(weeks)
            session.commit()

            match_weeks = [week.id for week in weeks if week.week_type == "match"][:2]
            rotating = teams[1:]
            for week_id in match_weeks:
                lineup = [teams[0], *rotating]
                for home, away in zip(lineup[:4], reversed(lineup[4:]), strict=True):
                    pairing, _ = create_team_match(session, week_id, home, away, clock=clock)
                    generate_player_matches(session, pairing.id, clock=clock)
                rotating = rotating[-1:] + rotating[:-1]
            session.commit()

            first_match = session.scalars(
                select(PlayerMatch).order_by(PlayerMatch.id)
            ).first()
            substitute_player(session, season.id, first_match.id, side="b", golfer_id=subs[0], clock=clock)

            links = {"regular": team_golfers[0], "substitute": subs[0], "unused": subs[1], "unlinked": None}
            now = datetime.now(UTC).replace(tzinfo=None)
            for persona, email in PERSONAS.items():
                session.add(User(username=email, email=email, display_name=f"Demo {persona}",
                                 password_hash=hash_password(DEMO_PASSWORD), email_verified_at=now,
                                 is_admin=False, golfer_id=links[persona]))
            session.commit()
            return True
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--demo", action="store_true", help="seed the synthetic demo season once")
    args = parser.parse_args(argv)

    os.environ.update(prepare_environment(args.data_dir, args.port))
    sys.path.insert(0, str(ROOT))

    import uvicorn

    from golf_league.app import create_app

    if args.demo and seed_demo(os.environ["DATABASE_URL"]):
        print("Seeded the synthetic demo season.")
    app = create_app()
    app.state.email_sender = FileMailSender(args.data_dir.resolve() / "mail")

    print(f"Golf League (local) at http://127.0.0.1:{args.port}")
    print(f"  first admin : {os.environ['ADMIN_EMAIL']} (password from ADMIN_PASSWORD, "
          f"default {DEFAULT_ADMIN_PASSWORD!r} when unset)")
    if args.demo:
        print(f"  personas    : {', '.join(PERSONAS.values())} (password {DEMO_PASSWORD!r})")
    print(f"  mail        : {args.data_dir.resolve() / 'mail'}")
    uvicorn.run(app, host="127.0.0.1", port=args.port, workers=1)


if __name__ == "__main__":
    main()
