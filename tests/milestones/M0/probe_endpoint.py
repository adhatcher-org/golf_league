"""Poll an HTTP endpoint with Python's stdlib urllib until it returns an
expected status code, or a timeout elapses.

Used by the M0 Docker health demo (see docker_health_demo.sh in this
directory) because `curl` is not available in the environment that runs it.
Never prints response bodies or headers, only the status code, so it cannot
leak anything sensitive a future endpoint might return.

Usage:
    python probe_endpoint.py <url> <expected_status> [timeout_seconds] [interval_seconds]

Exits 0 once `expected_status` is observed, or 1 if the timeout elapses
first.
"""

import sys
import time
import urllib.error
import urllib.request


def probe_once(url: str) -> int | None:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except urllib.error.URLError:
        return None


def main() -> int:
    url = sys.argv[1]
    expected_status = int(sys.argv[2])
    timeout_seconds = float(sys.argv[3]) if len(sys.argv) > 3 else 60.0
    interval_seconds = float(sys.argv[4]) if len(sys.argv) > 4 else 2.0

    deadline = time.monotonic() + timeout_seconds
    last_status: int | None = None
    while time.monotonic() < deadline:
        last_status = probe_once(url)
        if last_status == expected_status:
            print(f"OK {url} -> {last_status}")
            return 0
        time.sleep(interval_seconds)

    print(f"TIMEOUT {url} -> last status {last_status!r}, expected {expected_status}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
