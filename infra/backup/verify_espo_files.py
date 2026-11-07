"""Check the structure and readability of a trusted private EspoCRM file archive."""
import argparse
from pathlib import Path
import tarfile

from backup_espo_files import inspect_archive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    try:
        entries = inspect_archive(args.archive)
    except (OSError, ValueError, tarfile.TarError):
        raise SystemExit("CRM file archive failed validation.") from None
    print(f"CRM file archive validated: entries={entries}.")


if __name__ == "__main__":
    main()
