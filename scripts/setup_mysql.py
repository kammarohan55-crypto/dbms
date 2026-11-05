"""Provision a private Windows MySQL instance, without services or admin changes.

Official archive installation: https://dev.mysql.com/doc/refman/8.4/en/windows-install-archive.html
Only this project's .runtime directory, .env and loopback port 3307 are used.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime"
VERSION = "8.4.13"
URL = f"https://cdn.mysql.com/Downloads/MySQL-8.4/mysql-{VERSION}-winx64.zip"
ARCHIVE_MD5 = "7b94489d5524dea9b27097068bea25a8"
ARCHIVE = RUNTIME / "mysql.zip"
BASE = RUNTIME / f"mysql-{VERSION}-winx64"
MYSQLD = BASE / "bin" / "mysqld.exe"
DATA = RUNTIME / "mysql-data"
CONFIG = RUNTIME / "mysql.ini"
PID_FILE = RUNTIME / "mysqld.pid"
CREDENTIALS = RUNTIME / "mysql-credentials.json"
PORT = 3307
HIDDEN = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def download() -> None:
    if ARCHIVE.exists() and file_md5(ARCHIVE) == ARCHIVE_MD5:
        return
    offset = ARCHIVE.stat().st_size if ARCHIVE.exists() else 0
    print(f"Downloading official MySQL {VERSION} archive" + (" (resuming)." if offset else "."), flush=True)
    request = urllib.request.Request(URL, headers={"Range": f"bytes={offset}-"} if offset else {})
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            append = bool(offset and response.status == 206)
            with ARCHIVE.open("ab" if append else "wb") as output:
                while block := response.read(1024 * 1024):
                    output.write(block)
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeError("MySQL download was interrupted. Rerun this script to resume.") from exc
    if file_md5(ARCHIVE) != ARCHIVE_MD5:
        raise RuntimeError("MySQL archive checksum failed. Inspect .runtime/mysql.zip before retrying.")


def file_md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def extract() -> None:
    if MYSQLD.exists():
        return
    download()
    print("Extracting verified MySQL archive into this project.", flush=True)
    with zipfile.ZipFile(ARCHIVE) as archive:
        for member in archive.infolist():
            destination = (RUNTIME / member.filename).resolve()
            if not destination.is_relative_to(RUNTIME.resolve()):
                raise RuntimeError("Unsafe path in MySQL archive")
            if (member.external_attr >> 16) & 0o170000 == 0o120000:
                raise RuntimeError("Symbolic links are not accepted in MySQL archive")
        archive.extractall(RUNTIME)
    if not MYSQLD.exists():
        raise RuntimeError("Verified MySQL archive does not contain the expected server executable")


def port_open() -> bool:
    with socket.socket() as connection:
        connection.settimeout(.4)
        return connection.connect_ex(("127.0.0.1", PORT)) == 0


def own_process() -> int | None:
    if not PID_FILE.exists():
        return None
    try:
        pid = int(PID_FILE.read_text().strip())
        command = f"Get-CimInstance Win32_Process -Filter 'ProcessId={pid}' | Select-Object ExecutablePath,CommandLine | ConvertTo-Json -Compress"
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, creationflags=HIDDEN, timeout=15)
        details = json.loads(result.stdout or "null")
        if not details:
            return None
        executable = Path(details.get("ExecutablePath") or "").resolve()
        command_line = (details.get("CommandLine") or "").lower().replace("/", "\\")
        if executable == MYSQLD.resolve() and str(CONFIG.resolve()).lower() in command_line:
            return pid
    except (ValueError, OSError, subprocess.SubprocessError):
        return None
    return None


def write_config() -> None:
    CONFIG.write_text("\n".join([
        "[mysqld]", f"basedir={BASE.as_posix()}", f"datadir={DATA.as_posix()}",
        "bind-address=127.0.0.1", f"port={PORT}", "mysqlx=0", "skip-log-bin",
        "local-infile=OFF", "secure-file-priv=NULL", "character-set-server=utf8mb4",
        "collation-server=utf8mb4_0900_ai_ci", "max-connections=40", "innodb-buffer-pool-size=128M",
        f"pid-file={PID_FILE.as_posix()}", f"log-error={(RUNTIME / 'mysql-error.log').as_posix()}",
        "[client]", f"port={PORT}", "host=127.0.0.1", "",
    ]), encoding="utf-8")


def initialize() -> bool:
    if (DATA / "mysql").exists():
        return False
    if DATA.exists() and any(DATA.iterdir()):
        raise RuntimeError("The private MySQL datadir is nonempty but not initialized; inspect it before proceeding")
    DATA.mkdir(exist_ok=True)
    print("Initializing the project's isolated MySQL data directory.", flush=True)
    with (RUNTIME / "mysql-initialize.log").open("ab") as log:
        result = subprocess.run([str(MYSQLD), "--no-defaults", "--initialize-insecure",
            f"--basedir={BASE}", f"--datadir={DATA}"], stdout=log, stderr=log,
            creationflags=HIDDEN, timeout=180)
    if result.returncode:
        raise RuntimeError("MySQL initialization failed; inspect .runtime/mysql-initialize.log")
    return True


def root_sql(sql: str, password: str = "") -> bool:
    """Use the bundled client; secrets travel in protected files/stdin, not argv."""
    client_config = RUNTIME / "mysql-root-client.ini"
    client_config.write_text("\n".join(["[client]", "user=root", f"password={password}",
        "host=127.0.0.1", f"port={PORT}", "protocol=TCP", "connect-timeout=5", ""]), encoding="utf-8")
    secure_local_file(client_config)
    try:
        result = subprocess.run([str(BASE / "bin" / "mysql.exe"), f"--defaults-file={client_config}",
            "--batch", "--skip-column-names"], input=sql, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, creationflags=HIDDEN, timeout=45)
        # MySQL stderr can echo SQL. Never print it; a protected local diagnostic
        # file retains detail if an operator needs to inspect provisioning.
        if result.returncode:
            diagnostic = RUNTIME / "mysql-provision-error.log"
            diagnostic.write_text(result.stderr, encoding="utf-8")
            secure_local_file(diagnostic)
        return result.returncode == 0
    finally:
        client_config.unlink(missing_ok=True)


def start() -> None:
    if port_open():
        if own_process() is None:
            raise RuntimeError("Port 3307 is already occupied by an instance this project does not own")
        return
    write_config()
    with (RUNTIME / "mysql-console.log").open("ab") as log:
        process = subprocess.Popen([str(MYSQLD), f"--defaults-file={CONFIG}"], cwd=ROOT,
            stdin=subprocess.DEVNULL, stdout=log, stderr=log, creationflags=HIDDEN,
            close_fds=True)
    # The server writes the same PID; retaining it immediately allows ownership
    # checks during early startup and never targets an unrelated server.
    PID_FILE.write_text(str(process.pid), encoding="ascii")
    for _ in range(120):
        if port_open():
            return
        if process.poll() is not None:
            raise RuntimeError("MySQL exited during startup; inspect .runtime/mysql-error.log")
        time.sleep(.5)
    raise RuntimeError("Private MySQL did not become ready within 60 seconds; inspect its runtime logs")


def secure_local_file(path: Path) -> None:
    if os.name == "nt":
        identity = os.environ.get("USERDOMAIN", "") + "\\" + os.environ.get("USERNAME", "")
        subprocess.run(["icacls.exe", str(path), "/inheritance:r", "/grant:r", f"{identity}:(F)",
                        "/grant:r", "SYSTEM:(F)"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=HIDDEN, check=True)
    else:
        path.chmod(0o600)


def write_env(app_password: str) -> None:
    path = ROOT / ".env"
    existing = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
    updates = {"DATABASE_URL": f"mysql+pymysql://gridmind:{app_password}@127.0.0.1:{PORT}/gridmind"}
    # Preserve an explicitly selected external event backend; AWS remains opt-in.
    if not any(line.strip().startswith("EVENT_BACKEND=") for line in existing):
        updates["EVENT_BACKEND"] = "local"
    result, found = [], set()
    for line in existing:
        key = line.partition("=")[0].strip()
        if key in updates:
            result.append(f"{key}={updates[key]}")
            found.add(key)
        else:
            result.append(line)
    result.extend(f"{key}={value}" for key, value in updates.items() if key not in found)
    path.write_text("\n".join(result) + "\n", encoding="utf-8")
    secure_local_file(path)


def provision() -> None:
    if CREDENTIALS.exists():
        credentials = json.loads(CREDENTIALS.read_text(encoding="utf-8"))
        root_password = credentials["root_password"]
        if not root_sql("SELECT 1;", root_password):
            root_password = ""
    else:
        credentials = {"root_password": secrets.token_urlsafe(32), "app_password": secrets.token_urlsafe(32)}
        CREDENTIALS.write_text(json.dumps(credentials, indent=2), encoding="utf-8")
        secure_local_file(CREDENTIALS)
        root_password = ""
    # Generated passwords use only URL-safe token characters. Check persisted
    # inputs too before inserting them into the private SQL stdin stream.
    for value in credentials.values():
        if not value or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for character in value):
            raise RuntimeError("Unexpected characters in private generated credentials")
    statements = [f"ALTER USER 'root'@'localhost' IDENTIFIED BY '{credentials['root_password']}';",
        "CREATE DATABASE IF NOT EXISTS gridmind CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;"]
    for host in ("127.0.0.1", "localhost"):
        statements.extend([
            f"CREATE USER IF NOT EXISTS 'gridmind'@'{host}' IDENTIFIED BY '{credentials['app_password']}';",
            f"ALTER USER 'gridmind'@'{host}' IDENTIFIED BY '{credentials['app_password']}';",
            f"GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX, REFERENCES, CREATE VIEW, SHOW VIEW, TRIGGER ON gridmind.* TO 'gridmind'@'{host}';",
        ])
    if not root_sql("\n".join(statements), root_password):
        raise RuntimeError("Local MySQL provisioning failed; inspect the protected .runtime/mysql-provision-error.log")
    write_env(credentials["app_password"])
    print("Private MySQL is ready on 127.0.0.1:3307. Local credentials saved without printing them.", flush=True)


def stop() -> None:
    owned = own_process()
    if owned is None:
        if port_open():
            raise RuntimeError("Refusing to stop a MySQL instance whose executable and project configuration are not verified")
        print("The project's private MySQL is already stopped.")
        return
    if CREDENTIALS.exists():
        try:
            credentials = json.loads(CREDENTIALS.read_text(encoding="utf-8"))
            if not root_sql("SHUTDOWN;", credentials["root_password"]):
                raise RuntimeError("MySQL shutdown command failed")
        except Exception:
            # Verify ownership again before any OS-level termination.
            if own_process() != owned:
                raise RuntimeError("MySQL process ownership changed; stop aborted")
            subprocess.run(["taskkill.exe", "/F", "/PID", str(owned)], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, creationflags=HIDDEN, check=True)
    else:
        if own_process() != owned:
            raise RuntimeError("MySQL process ownership changed; stop aborted")
        subprocess.run(["taskkill.exe", "/F", "/PID", str(owned)], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, creationflags=HIDDEN, check=True)
    for _ in range(40):
        if not port_open():
            PID_FILE.unlink(missing_ok=True)
            print("The project's private MySQL stopped.")
            return
        time.sleep(.25)
    raise RuntimeError("Private MySQL shutdown is still pending; inspect runtime logs")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-only", action="store_true", help="Start an already-provisioned project MySQL")
    parser.add_argument("--stop", action="store_true", help="Stop only this project's verified MySQL process")
    args = parser.parse_args()
    if os.name != "nt":
        raise RuntimeError("This bundled MySQL setup targets Windows; configure DATABASE_URL for your existing MySQL on other platforms")
    for location in (RUNTIME, BASE, DATA, CONFIG, PID_FILE, CREDENTIALS):
        if not location.resolve().is_relative_to(ROOT.resolve()):
            raise RuntimeError("A private runtime path resolves outside this project; setup aborted")
    RUNTIME.mkdir(exist_ok=True)
    if args.stop:
        stop()
        return
    if args.start_only:
        if not MYSQLD.exists() or not CREDENTIALS.exists():
            raise RuntimeError("Private MySQL is not provisioned. Run scripts/setup_mysql.py first or scripts/start.ps1 -Setup")
        start()
        print("The project's private MySQL is running on 127.0.0.1:3307.")
        return
    extract()
    if port_open() and own_process() is None:
        raise RuntimeError("Port 3307 is occupied by an unrelated process; private setup will not alter it")
    initialize()
    start()
    provision()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Never render SQL statements or URLs that could contain credentials.
        print(f"Setup failed ({type(exc).__name__}). {str(exc) if isinstance(exc, RuntimeError) else 'Inspect local runtime logs for details.'}", file=sys.stderr)
        sys.exit(1)
