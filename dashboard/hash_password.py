"""Generate an ADMIN_PASSWORD_HASH value for the Koyura dashboard.

Usage:
    python -m hash_password              # prompts for a password
    python -m hash_password 'my secret'  # hashed for shell history
    docker compose run --rm dashboard python -m hash_password

The printed value is safe to place in .env as ADMIN_PASSWORD_HASH; the plain
password is never written to disk or logs.
"""
from __future__ import annotations

import getpass
import sys

from auth import hash_password


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        password = argv[1]
    else:
        password = getpass.getpass("Dashboard password: ")
        confirm = getpass.getpass("Repeat password: ")
        if password != confirm:
            print("Passwords do not match.", file=sys.stderr)
            return 1

    if len(password) < 8:
        print("Password must be at least 8 characters.", file=sys.stderr)
        return 1

    print(hash_password(password))
    print(
        "\nAdd this to .env (replace ADMIN_PASSWORD):\n"
        "ADMIN_PASSWORD_HASH=<the line above>",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
