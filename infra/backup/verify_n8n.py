"""Validate a trusted private n8n SQLite snapshot without starting n8n."""
import argparse
from pathlib import Path
import sqlite3

from backup_n8n import inspect_snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    args = parser.parse_args()
    try:
        workflows, credentials = inspect_snapshot(args.snapshot)
    except sqlite3.Error:
        raise SystemExit("n8n snapshot failed SQLite validation.") from None
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from None
    print(f"n8n snapshot verified: workflows={workflows}, credentials={credentials}.")


if __name__ == "__main__":
    main()
