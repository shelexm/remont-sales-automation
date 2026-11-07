"""Generate a private, local-only CA and certificate without overwriting keys."""
from pathlib import Path
import os
import subprocess

directory = Path(__file__).resolve().parent / ".local"
directory.mkdir(mode=0o700, exist_ok=True)
os.chmod(directory, 0o700)
files = [directory / name for name in ("ca.key", "ca.crt", "crm.key", "crm.csr", "crm.crt", "ca.srl")]
if any(path.exists() for path in files):
    raise SystemExit("TLS files already exist; no files were changed. Back up and rotate them explicitly.")

def openssl(*args):
    subprocess.run(["openssl", *args], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

try:
    old_umask = os.umask(0o077)
    openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(files[0]),
            "-out", str(files[1]), "-days", "365", "-subj", "/CN=Remont Local Demo CA",
            "-addext", "basicConstraints=critical,CA:TRUE")
    openssl("req", "-newkey", "rsa:2048", "-nodes", "-keyout", str(files[2]),
            "-out", str(files[3]), "-subj", "/CN=crm.remont.internal",
            "-addext", "subjectAltName=DNS:crm.remont.internal")
    openssl("x509", "-req", "-in", str(files[3]), "-CA", str(files[1]), "-CAkey", str(files[0]),
            "-CAcreateserial", "-out", str(files[4]), "-days", "90", "-copy_extensions", "copy")
    files[1].chmod(0o644)
    files[4].chmod(0o644)
finally:
    os.umask(old_umask)
print("Local TLS certificates created. Protect the .local directory.")
