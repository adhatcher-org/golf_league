# Backup and restore

What must be backed up, all of it inside the mounted data directory (`/app/data` in the
container, `GOLF_LEAGUE_DATA_DIR` on the host):

- `golf_league.db` — the SQLite database.
- `.env` — the managed settings file (`MANAGED_ENV_PATH`). It can hold the SMTP password, so
  the copy is private: keep mode 0600 and the owner.
- The private deployment values (`SESSION_SECRET` and the compose env file) are kept by the
  operator separately. Restoring a database with a different `SESSION_SECRET` signs everyone
  out and resets invite-quota keys; it does not lose data.

Never copy the live `golf_league.db` file on its own while the app is running: a copy taken
mid-write can be inconsistent, and SQLite may also have `-wal`/`-journal` files beside it.

## Option A — online, transactionally consistent (SQLite backup API)

```bash
docker exec golf-league python -c "
import sqlite3, os, datetime
os.makedirs('/app/data/backups', exist_ok=True)
stamp = datetime.datetime.now(datetime.UTC).strftime('%Y%m%dT%H%M%SZ')
src = sqlite3.connect('/app/data/golf_league.db')
dst = sqlite3.connect(f'/app/data/backups/golf_league-{stamp}.db')
with dst:
    src.backup(dst)
print(stamp, dst.execute('PRAGMA integrity_check').fetchone()[0])
dst.close(); src.close()"
cp -p <GOLF_LEAGUE_DATA_DIR>/.env <GOLF_LEAGUE_DATA_DIR>/backups/managed-env-<stamp>
```

`Connection.backup` copies a consistent snapshot even while requests are writing. Expect
`integrity_check` to print `ok`. Then copy the `backups/` directory off the server (the Unraid
array share used for backups) with permissions preserved (`cp -p`, `rsync -a`).

## Option B — stop and copy

```bash
docker compose stop golf-league
cp -p <GOLF_LEAGUE_DATA_DIR>/golf_league.db* <GOLF_LEAGUE_DATA_DIR>/.env <backup location>/
docker compose start golf-league
```

Copy every `golf_league.db*` file while the container is stopped.

## Restore

1. Stop the app: `docker compose stop golf-league`.
2. Keep the current files aside (do not delete them until the restore is verified).
3. Copy the backup database to `<GOLF_LEAGUE_DATA_DIR>/golf_league.db` and the managed settings
   copy to `<GOLF_LEAGUE_DATA_DIR>/.env`; remove any stale `golf_league.db-wal` /
   `golf_league.db-journal`; restore owner `PUID:PGID` and mode 0600 on `.env`.
4. Start an image at least as new as the one that wrote the backup. Startup runs
   `alembic upgrade head`, which brings an older backup forward; an older image cannot read a
   newer schema, and there is no downgrade.
5. Verify: container healthy, `/readyz` 200, admin login works, a known row (for example the
   season list) is present, `/admin/config` shows the expected saved values.

## Practise first

Before relying on a backup, restore it into an isolated directory and a throwaway container
(different name, no published production port, synthetic or copied data only), check the five
points above, then remove the throwaway container. Never test a restore over the production
directory.

## Data-loss window and rollback

- The loss window is the time since the last backup. Take one before every image upgrade and on
  a schedule the owner chooses (for example nightly, using the Unraid user-scripts plugin to run
  Option A and copy the result to the array).
- Rolling back an upgrade = stop, restore the backup taken before it, start the previous image
  digest. Never run an older image against a database a newer image has migrated.
