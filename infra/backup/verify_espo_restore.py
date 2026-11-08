"""Test trusted EspoCRM database and file backups together, without external networking."""
import argparse
import os
from pathlib import Path
import secrets
import stat
import subprocess
import tarfile
import time
import uuid

from backup_espo_files import inspect_archive
from backup_mariadb import has_footer

CRM_IMAGE = "espocrm:10.0.11"
DB_IMAGE = "mariadb:11.4"


def validate_inputs(database, files):
    database = Path(database).expanduser()
    files = Path(files).expanduser()
    if database.is_symlink():
        raise ValueError("Database backup must not be a symlink.")
    info = database.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Database backup must be a private regular file owned by the current user (0600).")
    if not has_footer(database):
        raise ValueError("Database backup is incomplete.")
    inspect_archive(files)
    with tarfile.open(files, "r:") as archive:
        if any(item.issym() or item.islnk() for item in archive):
            raise ValueError("CRM file archive contains links; this isolated check requires link-free archives.")
    return database, files


def run(command, *, timeout=30, **kwargs):
    result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout, **kwargs)
    return result.returncode == 0


def verify(database, files):
    database, files = validate_inputs(database, files)
    suffix = uuid.uuid4().hex
    db_name, crm_name = "remont-espo-check-db-" + suffix, "remont-espo-check-app-" + suffix
    db_password, root_password = secrets.token_hex(24), secrets.token_hex(24)
    db_env = {**os.environ, "MARIADB_DATABASE": "espocrm", "MARIADB_USER": "restorecheck",
              "MARIADB_PASSWORD": db_password, "MARIADB_ROOT_PASSWORD": root_password}
    try:
        # A network-none MariaDB namespace is shared with the temporary app, so only loopback exists.
        if not run(["docker", "run", "--rm", "-d", "--name", db_name, "--network", "none",
                    "--memory", "512m", "--cpus", "1", "--tmpfs", "/var/lib/mysql:rw,nosuid,nodev,size=512m",
                    "-e", "MARIADB_DATABASE", "-e", "MARIADB_USER", "-e", "MARIADB_PASSWORD",
                    "-e", "MARIADB_ROOT_PASSWORD", DB_IMAGE], env=db_env, timeout=90):
            raise RuntimeError("Could not start isolated MariaDB.")
        probe = ('MYSQL_PWD="$MARIADB_ROOT_PASSWORD" exec mariadb-admin '
                 '--protocol=tcp --host=127.0.0.1 --user=root ping')
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if run(["docker", "exec", db_name, "sh", "-c", probe], timeout=5):
                break
            time.sleep(1)
        else:
            raise RuntimeError("Isolated MariaDB did not become ready.")
        restore = 'MYSQL_PWD="$MARIADB_ROOT_PASSWORD" exec mariadb --user=root --database=espocrm'
        with database.open("rb") as source:
            if not run(["docker", "exec", "-i", db_name, "sh", "-c", restore], stdin=source, timeout=300):
                raise RuntimeError("Could not restore MariaDB backup.")
        app_env = {**os.environ, "RESTORE_DB_PASSWORD": db_password}
        if not run(["docker", "run", "--rm", "-d", "--name", crm_name, "--network", "container:" + db_name,
                    "--memory", "384m", "--cpus", "1", "--tmpfs", "/var/www/html/data:rw,nosuid,nodev,size=256m",
                    "--tmpfs", "/var/www/html/custom:rw,nosuid,nodev,size=128m",
                    "--tmpfs", "/var/www/html/client/custom:rw,nosuid,nodev,size=128m",
                    "-e", "RESTORE_DB_PASSWORD", "--entrypoint", "sh", CRM_IMAGE, "-c", "sleep 600"],
                   env=app_env, timeout=90):
            raise RuntimeError("Could not start isolated EspoCRM container.")
        with files.open("rb") as source:
            if not run(["docker", "exec", "-i", crm_name, "tar", "--no-same-owner", "-C", "/var/www/html", "-xf", "-"],
                       stdin=source, timeout=180):
                raise RuntimeError("Could not restore the CRM file archive.")
        # Edit only the disposable config. Original CRM credentials and paths are never printed or used.
        php = ("$p='/var/www/html/data/config-internal.php'; $c=require $p; "
               "if(!is_array($c) || !isset($c['database'])) exit(1); "
               "$c['database']['host']='127.0.0.1'; $c['database']['port']='3306'; "
               "$c['database']['dbname']='espocrm'; $c['database']['user']='restorecheck'; "
               "$c['database']['password']=getenv('RESTORE_DB_PASSWORD'); "
               "if(file_put_contents($p,'<?php return '.var_export($c,true).';')===false) exit(1);")
        if not run(["docker", "exec", crm_name, "php", "-r", php], timeout=30):
            raise RuntimeError("Could not configure the isolated CRM copy.")
        if not run(["docker", "exec", crm_name, "chown", "-R", "www-data:www-data", "/var/www/html/data",
                    "/var/www/html/custom", "/var/www/html/client/custom"], timeout=60):
            raise RuntimeError("Could not set isolated CRM file ownership.")
        if not run(["docker", "exec", "--user", "www-data", "--workdir", "/var/www/html", crm_name,
                    "bin/command", "app-check"], timeout=90):
            raise RuntimeError("Restored CRM failed its application and database check.")
        if not run(["docker", "exec", "-d", crm_name, "apache2-foreground"], timeout=15):
            raise RuntimeError("Could not start isolated CRM web server.")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if run(["docker", "exec", crm_name, "curl", "--fail", "--silent", "--max-time", "3",
                    "--output", "/dev/null", "http://127.0.0.1/"], timeout=5):
                break
            time.sleep(1)
        else:
            raise RuntimeError("Restored CRM web page did not respond in the isolated container.")
        query = "SELECT (SELECT COUNT(*) FROM contact), (SELECT COUNT(*) FROM opportunity)"
        check = ('MYSQL_PWD="$MARIADB_ROOT_PASSWORD" exec mariadb --user=root --database=espocrm '
                 '--batch --skip-column-names --execute="$1"')
        checked = subprocess.run(["docker", "exec", db_name, "sh", "-c", check, "sh", query],
                                 capture_output=True, text=True, timeout=30)
        counts = checked.stdout.strip().split("\t")
        if checked.returncode or len(counts) != 2 or any(not x.isdecimal() for x in counts):
            raise RuntimeError("Restored CRM tables could not be checked.")
        print(f"Isolated EspoCRM check passed: contacts={int(counts[0])}, opportunities={int(counts[1])}.")
        print("Application, database and local HTTP responded; outbound networking and published ports were disabled.")
    finally:
        failures = []
        for name in (crm_name, db_name):
            try:
                removed = run(["docker", "rm", "--force", "--volumes", name], timeout=30)
                remains = run(["docker", "inspect", "--type", "container", name], timeout=15)
                if not removed and not remains and not run(["docker", "info"], timeout=15):
                    failures.append(name)
                    continue
            except (OSError, subprocess.TimeoutExpired):
                failures.append(name)
                continue
            if remains:
                failures.append(name)
        if failures:
            raise RuntimeError("Could not remove disposable containers: " + ", ".join(failures))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("files", type=Path)
    args = parser.parse_args()
    try:
        verify(args.database, args.files)
    except subprocess.TimeoutExpired:
        raise SystemExit("Isolated CRM verification timed out; check Docker for disposable containers.") from None
    except tarfile.TarError:
        raise SystemExit("CRM file archive failed validation.") from None
    except (OSError, ValueError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()
