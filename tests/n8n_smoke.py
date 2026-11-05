"""CI-only smoke test: disposable n8n container and isolated CI PostgreSQL."""
import json
import os
import subprocess
import tempfile
from pathlib import Path

import psycopg

root = Path(__file__).resolve().parents[1]
dsn = os.environ["TEST_DATABASE_URL"]
workflow = json.loads((root / "workflows/process-leads.json").read_text())
workflow["id"] = "ciLeadWorkflow"
workflow["nodes"][0].update({"type": "n8n-nodes-base.manualTrigger", "typeVersion": 1, "parameters": {}})
for node in workflow["nodes"][1:]:
    node["credentials"] = {"postgres": {"id": "testPostgres", "name": "testPostgres"}}
credential = [{"id": "testPostgres", "name": "testPostgres", "type": "postgres", "data": {
    "host": "127.0.0.1", "port": 5432, "database": "test", "user": "test", "password": "test", "ssl": "disable"}}]
with psycopg.connect(dsn) as conn:
    conn.execute("TRUNCATE lead_events, lead_cases RESTART IDENTITY")
    conn.execute("""INSERT INTO lead_events(source_id, payload_hash, payload)
        VALUES ('smoke', %s, '{"phone":"+79991234567","service":"capital"}')""", ("a" * 64,))
with tempfile.TemporaryDirectory() as tmp:
    fixture = Path(tmp)
    fixture.chmod(0o755)
    (fixture / "workflow.json").write_text(json.dumps(workflow, ensure_ascii=False))
    (fixture / "credentials.json").write_text(json.dumps(credential))
    subprocess.run([
        "docker", "run", "--rm", "--network", "host",
        "-e", "N8N_ENCRYPTION_KEY=temporary_ci_key_for_testing_only_123456",
        "-e", "N8N_DIAGNOSTICS_ENABLED=false", "-v", f"{tmp}:/fixtures:ro",
        "--entrypoint", "/bin/sh", "docker.n8n.io/n8nio/n8n:2.46.6", "-c",
        "n8n import:credentials --input=/fixtures/credentials.json >/dev/null && n8n import:workflow --input=/fixtures/workflow.json >/dev/null && n8n execute --id=ciLeadWorkflow",
    ], check=True, timeout=240)
with psycopg.connect(dsn) as conn:
    assert conn.execute("SELECT status FROM lead_events WHERE source_id='smoke'").fetchone() == ("completed",)
    assert conn.execute("SELECT count(*) FROM lead_cases").fetchone() == (1,)
print("n8n processed a test lead successfully")
