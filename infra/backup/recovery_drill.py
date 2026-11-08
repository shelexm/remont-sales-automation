"""Exercise restored PostgreSQL, n8n and EspoCRM together without outbound networking."""
import argparse
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import subprocess
import tempfile
import time
from urllib.parse import quote
import uuid

from backup_n8n import inspect_snapshot
from backup_postgres import container_name
from n8n_keys import read_key_file, validate_key
from recovery_config import read_config, secret_env, validate_config
from verify_espo_restore import validate_inputs

N8N_IMAGE = "docker.n8n.io/n8nio/n8n:2.46.6"
PG_IMAGE = "postgres:17-alpine"
DB_IMAGE = "mariadb:11.4"
CRM_IMAGE = "espocrm:10.0.11"
NGINX_IMAGE = "nginx:1.27-alpine"
RECEIVER_IMAGE = "remont-trial-receiver:latest"
WORKER_IMAGE = "remont-trial-integrations:latest"


def run(args, *, timeout=30, **kwargs):
    return subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          timeout=timeout, **kwargs).returncode == 0


def captured(args, *, timeout=30, **kwargs):
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout, **kwargs)
    if result.returncode:
        raise RuntimeError("Isolated recovery operation failed.")
    return result.stdout


def private_file(path):
    path = Path(path).expanduser()
    if path.is_symlink():
        raise ValueError("Snapshot must not be a symlink.")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Snapshot must be a private regular file owned by the current user (0600).")
    return path


def validate_n8n_snapshot(path):
    if path.stat().st_size > 128 * 1024 * 1024:
        raise ValueError("n8n snapshot is too large for the isolated drill.")
    inspect_snapshot(path)
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True) as conn:
        workflows = conn.execute("SELECT id, active FROM workflow_entity").fetchall()
    if workflows != [("remontAutoLocal", 1)]:
        raise ValueError("Drill requires exactly the expected active demo workflow; no workflows were started.")


def wait_until(check, seconds, message):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(1)
    raise RuntimeError(message)


def postgres_query(container, sql):
    command = 'PGPASSWORD="$POSTGRES_PASSWORD" exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -v ON_ERROR_STOP=1'
    return captured(["docker", "exec", "-i", container, "sh", "-c", command],
                    input=sql.encode(), timeout=15).decode().strip()


def put(container, target, content):
    command = 'umask 077; cat > "$1"'
    if not run(["docker", "exec", "-i", "--user", "0:0", container, "sh", "-c", command, "sh", target],
               input=content, timeout=30):
        raise RuntimeError("Could not populate a temporary container.")


def drill(postgres, crm_db, crm_files, n8n_db, n8n_key, source_db=None, source_integrations=None, config_file=None):
    postgres = private_file(postgres)
    crm_db, crm_files = validate_inputs(crm_db, crm_files)
    n8n_db = private_file(n8n_db)
    validate_n8n_snapshot(n8n_db)
    key = validate_key(read_key_file(n8n_key))
    if config_file is not None:
        if source_db is not None or source_integrations is not None:
            raise ValueError("Use either a recovery configuration file or both source containers.")
        config = read_config(config_file)
    else:
        if not source_db or not source_integrations:
            raise ValueError("Use either a recovery configuration file or both source containers.")
        config = validate_config({"version": 1, **secret_env(source_db, ["POSTGRES_PASSWORD"]),
                                  **secret_env(source_integrations, ["ESPO_URL", "ESPO_API_KEY", "ESPO_ASSIGNED_USER_ID"])})
    pg_password = config["POSTGRES_PASSWORD"]
    crm_config = {name: config[name] for name in ("ESPO_URL", "ESPO_API_KEY", "ESPO_ASSIGNED_USER_ID")}
    for image in (N8N_IMAGE, PG_IMAGE, DB_IMAGE, CRM_IMAGE, NGINX_IMAGE, RECEIVER_IMAGE, WORKER_IMAGE):
        if not run(["docker", "image", "inspect", image], timeout=15):
            raise RuntimeError("A required local Docker image is missing; no images were pulled.")

    suffix = uuid.uuid4().hex
    names = {key: "remont-drill-" + key + "-" + suffix for key in ("pg", "mysql", "crm", "proxy", "n8n", "receiver", "worker")}
    created = []
    mysql_password, mysql_root, token = secrets.token_hex(24), secrets.token_hex(24), secrets.token_hex(24)
    dsn = "postgresql://remont:" + quote(pg_password, safe="") + "@127.0.0.1:5432/remont"
    try:
        pg_env = {**os.environ, "POSTGRES_USER": "remont", "POSTGRES_DB": "remont", "POSTGRES_PASSWORD": pg_password}
        pg_cmd = ["docker", "run", "--rm", "-d", "--name", names["pg"], "--network", "none",
                  "--add-host", "remont-trial-db-1:127.0.0.1", "--add-host", "crm.remont.internal:127.0.0.1",
                  "--memory", "512m", "--cpus", "1", "--tmpfs", "/var/lib/postgresql/data:rw,nosuid,nodev,size=512m",
                  "-e", "POSTGRES_USER", "-e", "POSTGRES_DB", "-e", "POSTGRES_PASSWORD", PG_IMAGE]
        created.append(names["pg"])
        if not run(pg_cmd, env=pg_env, timeout=90):
            raise RuntimeError("Could not start isolated PostgreSQL.")
        wait_until(lambda: run(["docker", "exec", names["pg"], "pg_isready", "-h", "127.0.0.1", "-U", "remont", "-d", "remont"], timeout=5),
                   60, "Isolated PostgreSQL did not become ready.")
        restore = 'PGPASSWORD="$POSTGRES_PASSWORD" exec pg_restore --exit-on-error --no-owner --no-privileges -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
        with postgres.open("rb") as source:
            if not run(["docker", "exec", "-i", names["pg"], "sh", "-c", restore], stdin=source, timeout=300):
                raise RuntimeError("Could not restore PostgreSQL backup.")
        pending = postgres_query(names["pg"], "SELECT (SELECT count(*) FROM lead_events WHERE status IN ('pending','processing')) + "
                                            "(SELECT count(*) FROM integration_jobs WHERE destination='crm' AND status IN ('pending','processing'));")
        if pending != "0":
            raise RuntimeError("Backup contains pending lead or CRM work; drill refuses to replay it.")

        mysql_env = {**os.environ, "MARIADB_DATABASE": "espocrm", "MARIADB_USER": "restorecheck",
                     "MARIADB_PASSWORD": mysql_password, "MARIADB_ROOT_PASSWORD": mysql_root}
        mysql_cmd = ["docker", "run", "--rm", "-d", "--name", names["mysql"], "--network", "container:" + names["pg"],
                     "--memory", "512m", "--cpus", "1", "--tmpfs", "/var/lib/mysql:rw,nosuid,nodev,size=512m",
                     "-e", "MARIADB_DATABASE", "-e", "MARIADB_USER", "-e", "MARIADB_PASSWORD", "-e", "MARIADB_ROOT_PASSWORD", DB_IMAGE]
        created.append(names["mysql"])
        if not run(mysql_cmd, env=mysql_env, timeout=90):
            raise RuntimeError("Could not start isolated CRM database.")
        mysql_probe = 'MYSQL_PWD="$MARIADB_ROOT_PASSWORD" exec mariadb-admin --protocol=tcp --host=127.0.0.1 --user=root ping'
        wait_until(lambda: run(["docker", "exec", names["mysql"], "sh", "-c", mysql_probe], timeout=5),
                   90, "Isolated CRM database did not become ready.")
        mysql_restore = 'MYSQL_PWD="$MARIADB_ROOT_PASSWORD" exec mariadb --user=root --database=espocrm'
        with crm_db.open("rb") as source:
            if not run(["docker", "exec", "-i", names["mysql"], "sh", "-c", mysql_restore], stdin=source, timeout=300):
                raise RuntimeError("Could not restore CRM database.")

        app_env = {**os.environ, "RESTORE_DB_PASSWORD": mysql_password}
        crm_cmd = ["docker", "run", "--rm", "-d", "--name", names["crm"], "--network", "container:" + names["pg"],
                   "--memory", "384m", "--cpus", "1", "--tmpfs", "/var/www/html/data:rw,nosuid,nodev,size=256m",
                   "--tmpfs", "/var/www/html/custom:rw,nosuid,nodev,size=128m", "--tmpfs", "/var/www/html/client/custom:rw,nosuid,nodev,size=128m",
                   "-e", "RESTORE_DB_PASSWORD", "--entrypoint", "sh", CRM_IMAGE, "-c", "sleep 600"]
        created.append(names["crm"])
        if not run(crm_cmd, env=app_env, timeout=90):
            raise RuntimeError("Could not start isolated CRM.")
        with crm_files.open("rb") as source:
            if not run(["docker", "exec", "-i", names["crm"], "tar", "--no-same-owner", "-C", "/var/www/html", "-xf", "-"],
                       stdin=source, timeout=180):
                raise RuntimeError("Could not restore CRM files.")
        php = ("$p='/var/www/html/data/config-internal.php'; $c=require $p; "
               "if(!is_array($c)||!isset($c['database']))exit(1); $c['database']['host']='127.0.0.1'; "
               "$c['database']['port']='3306'; $c['database']['dbname']='espocrm'; $c['database']['user']='restorecheck'; "
               "$c['database']['password']=getenv('RESTORE_DB_PASSWORD'); "
               "if(file_put_contents($p,'<?php return '.var_export($c,true).';')===false)exit(1);")
        if not run(["docker", "exec", names["crm"], "php", "-r", php], timeout=30):
            raise RuntimeError("Could not configure the isolated CRM copy.")
        if not run(["docker", "exec", names["crm"], "chown", "-R", "www-data:www-data", "/var/www/html/data",
                    "/var/www/html/custom", "/var/www/html/client/custom"], timeout=60):
            raise RuntimeError("Could not set isolated CRM ownership.")
        if not run(["docker", "exec", "--user", "www-data", "--workdir", "/var/www/html", names["crm"], "bin/command", "app-check"], timeout=90):
            raise RuntimeError("Restored CRM failed its application check.")
        if not run(["docker", "exec", "-d", names["crm"], "apache2-foreground"], timeout=15):
            raise RuntimeError("Could not start isolated CRM HTTP server.")
        wait_until(lambda: run(["docker", "exec", names["crm"], "curl", "--fail", "--silent", "--max-time", "3",
                                "--output", "/dev/null", "http://127.0.0.1/"], timeout=5), 30, "CRM HTTP did not become ready.")

        with tempfile.TemporaryDirectory(prefix="remont-drill-tls-") as directory:
            directory = Path(directory)
            certificate, private_key = directory / "cert.pem", directory / "key.pem"
            if not run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-keyout", str(private_key),
                        "-out", str(certificate), "-subj", "/CN=crm.remont.internal", "-addext", "subjectAltName=DNS:crm.remont.internal"], timeout=60):
                raise RuntimeError("Could not create temporary TLS certificate.")
            private_key.chmod(0o600)
            proxy_cmd = ["docker", "run", "--rm", "-d", "--name", names["proxy"], "--network", "container:" + names["pg"],
                         "--memory", "128m", "--tmpfs", "/run/tls:rw,nosuid,nodev,size=1m", "--entrypoint", "sh", NGINX_IMAGE, "-c", "sleep 600"]
            created.append(names["proxy"])
            if not run(proxy_cmd, timeout=60):
                raise RuntimeError("Could not start isolated CRM TLS proxy.")
            put(names["proxy"], "/run/tls/key.pem", private_key.read_bytes())
            put(names["proxy"], "/run/tls/cert.pem", certificate.read_bytes())
            config = b'worker_processes 1; events { worker_connections 32; } http { access_log off; server { listen 8443 ssl; server_name crm.remont.internal; ssl_certificate /run/tls/cert.pem; ssl_certificate_key /run/tls/key.pem; location / { proxy_pass http://127.0.0.1:80; } } }'
            put(names["proxy"], "/etc/nginx/nginx.conf", config)
            if not run(["docker", "exec", "-d", names["proxy"], "nginx", "-g", "daemon off;"], timeout=15):
                raise RuntimeError("Could not start isolated TLS proxy.")

            n8n_env = {**os.environ, "N8N_DIAGNOSTICS_ENABLED": "false",
                       "N8N_PERSONALIZATION_ENABLED": "false", "N8N_VERSION_NOTIFICATIONS_ENABLED": "false", "N8N_LOG_LEVEL": "error"}
            n8n_env["N8N_ENCRYPTION_KEY"] = key
            n8n_cmd = ["docker", "run", "--rm", "-d", "--name", names["n8n"], "--network", "container:" + names["pg"],
                       "--user", "0:0", "--memory", "1200m", "--cpus", "2", "--tmpfs", "/home/node/.n8n:rw,nosuid,nodev,size=256m",
                       "-e", "N8N_ENCRYPTION_KEY", "-e", "N8N_DIAGNOSTICS_ENABLED", "-e", "N8N_PERSONALIZATION_ENABLED",
                       "-e", "N8N_VERSION_NOTIFICATIONS_ENABLED", "-e", "N8N_LOG_LEVEL", "--entrypoint", "sh", N8N_IMAGE, "-c", "sleep 600"]
            created.append(names["n8n"])
            if not run(n8n_cmd, env=n8n_env, timeout=90):
                raise RuntimeError("Could not start isolated n8n container.")
            put(names["n8n"], "/home/node/.n8n/database.sqlite", n8n_db.read_bytes())
            if not run(["docker", "exec", names["n8n"], "chown", "-R", "node:node", "/home/node/.n8n"], timeout=30):
                raise RuntimeError("Could not set isolated n8n ownership.")
            if not run(["docker", "exec", "-d", "--user", "node", names["n8n"], "n8n", "start"], timeout=15):
                raise RuntimeError("Could not start restored n8n.")
            health = "require('http').get('http://127.0.0.1:5678/healthz',r=>process.exit(r.statusCode===200?0:1)).on('error',()=>process.exit(1))"
            wait_until(lambda: run(["docker", "exec", "--user", "node", names["n8n"], "node", "-e", health], timeout=5),
                       90, "Restored n8n did not become ready.")

            receiver_env = {**os.environ, "DATABASE_URL": dsn, "WEBHOOK_KEY": token, "HIGH_BUDGET_THRESHOLD": "1000000"}
            receiver_cmd = ["docker", "run", "--rm", "-d", "--name", names["receiver"], "--network", "container:" + names["pg"],
                            "--memory", "256m", "-e", "DATABASE_URL", "-e", "WEBHOOK_KEY", "-e", "HIGH_BUDGET_THRESHOLD", RECEIVER_IMAGE]
            created.append(names["receiver"])
            if not run(receiver_cmd, env=receiver_env, timeout=60):
                raise RuntimeError("Could not start isolated receiver.")
            ready = "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/ready',timeout=3).read()"
            wait_until(lambda: run(["docker", "exec", names["receiver"], "python", "-c", ready], timeout=5),
                       30, "Isolated receiver did not become ready.")

            worker_env = {**os.environ, **crm_config, "DATABASE_URL": dsn, "CRM_PROVIDER": "espocrm", "TELEGRAM_ENABLED": "false",
                          "CRM_ADOPT_EXISTING": "true", "CRM_ADOPT_MIXED_HISTORY": "true", "SSL_CERT_FILE": "/run/tls/ca.crt"}
            worker_cmd = ["docker", "run", "--rm", "-d", "--name", names["worker"], "--network", "container:" + names["pg"],
                          "--user", "0:0", "--memory", "256m", "--tmpfs", "/run/tls:rw,nosuid,nodev,size=1m"]
            for variable in ("DATABASE_URL", "CRM_PROVIDER", "TELEGRAM_ENABLED", "CRM_ADOPT_EXISTING", "CRM_ADOPT_MIXED_HISTORY",
                             "ESPO_URL", "ESPO_API_KEY", "ESPO_ASSIGNED_USER_ID", "SSL_CERT_FILE"):
                worker_cmd += ["-e", variable]
            worker_cmd += ["--entrypoint", "sh", WORKER_IMAGE, "-c", "sleep 600"]
            created.append(names["worker"])
            if not run(worker_cmd, env=worker_env, timeout=60):
                raise RuntimeError("Could not start isolated integration container.")
            put(names["worker"], "/run/tls/ca.crt", certificate.read_bytes())
            if not run(["docker", "exec", "-d", names["worker"], "python", "-u", "-m", "app.integrations.worker"], timeout=15):
                raise RuntimeError("Could not start isolated integration worker.")

            source_id = "recovery-drill-" + uuid.uuid4().hex
            fake = {"tranid": source_id, "Name": "Тест восстановления", "Phone": "+79990000867", "Service": "capital",
                    "Area": "67", "Budget": "1700000", "Consent": "yes"}
            send = ("import json,os,sys,urllib.parse,urllib.request; p=json.load(sys.stdin); "
                    "r=urllib.request.Request('http://127.0.0.1:8080/webhook/tilda',data=urllib.parse.urlencode(p).encode(),"
                    "headers={'X-Webhook-Key':os.environ['WEBHOOK_KEY'],'Content-Type':'application/x-www-form-urlencoded'}); "
                    "v=json.load(urllib.request.urlopen(r,timeout=8)); sys.exit(0 if v.get('status')=='accepted' else 1)")
            if not run(["docker", "exec", "-i", names["receiver"], "python", "-c", send],
                       input=json.dumps(fake).encode(), timeout=15):
                raise RuntimeError("Synthetic webhook was not accepted.")
            query = ("SELECT e.status || '|' || COALESCE(j.status,'missing') || '|' || COALESCE(t.remote_id,'-') "
                     "FROM lead_events e LEFT JOIN integration_jobs j ON j.event_id=e.id AND j.destination='crm' "
                     "LEFT JOIN crm_tracking t ON t.case_id=e.case_id WHERE e.source_id='" + source_id + "';")
            result = [""]

            def delivered():
                result[0] = postgres_query(names["pg"], query)
                return result[0].startswith("completed|done|") and not result[0].endswith("|-")

            wait_until(delivered, 180, "Restored chain did not complete the synthetic CRM job in time.")
            remote_id = result[0].split("|")[-1]
            if not re.fullmatch(r"[A-Za-z0-9]+", remote_id):
                raise RuntimeError("Restored CRM returned an invalid deal identifier.")
            check = ("import os,sys; from app.integrations.providers import EspoCRM; "
                     "c=EspoCRM(os.environ['ESPO_URL'],os.environ['ESPO_API_KEY'],None,os.environ['ESPO_ASSIGNED_USER_ID']); "
                     "sys.exit(0 if c.get_stage(sys.argv[1]) else 1)")
            if not run(["docker", "exec", names["worker"], "python", "-c", check, remote_id], timeout=20):
                raise RuntimeError("Created synthetic deal could not be read from restored CRM.")
        print("Recovery drill passed: restored receiver → n8n → PostgreSQL → EspoCRM created a synthetic deal.")
        print("No external networking, real Telegram delivery or live data volumes were used.")
    finally:
        failures = []
        for name in reversed(created):
            try:
                removed = run(["docker", "rm", "--force", "--volumes", name], timeout=30)
                remains = run(["docker", "inspect", "--type", "container", name], timeout=15)
                if remains or (not removed and not run(["docker", "info"], timeout=15)):
                    failures.append(name)
            except (OSError, subprocess.TimeoutExpired):
                failures.append(name)
        if failures:
            raise RuntimeError("Could not remove disposable containers: " + ", ".join(failures))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("postgres", "crm-db", "crm-files", "n8n-db", "n8n-key"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--source-db", type=container_name)
    parser.add_argument("--source-integrations", type=container_name)
    parser.add_argument("--config-file", type=Path)
    args = parser.parse_args()
    try:
        drill(args.postgres, args.crm_db, args.crm_files, args.n8n_db, args.n8n_key, args.source_db,
              args.source_integrations, args.config_file)
    except subprocess.TimeoutExpired:
        raise SystemExit("Recovery drill timed out; inspect Docker for temporary remont-drill containers.") from None
    except sqlite3.Error:
        raise SystemExit("n8n snapshot failed validation.") from None
    except (OSError, ValueError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()
