"""Print the Alembic revision stamped in a SQLite database file.

Used by the M0 Docker health demo to prove the database persists across
`docker compose restart`: the revision (and the file itself) must be
identical before and after. Reads the file directly since it is bind-mounted
onto the host at ./data, next to docker-compose.yml.

Usage:
    python read_alembic_version.py <sqlite_path>
"""

import sqlite3
import sys


def main() -> int:
    db_path = sys.argv[1]
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute("SELECT version_num FROM alembic_version")
        row = cursor.fetchone()
    finally:
        conn.close()
    print(row[0] if row else "NONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
