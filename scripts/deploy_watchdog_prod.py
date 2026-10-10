from __future__ import annotations

import argparse
import base64
import re
import subprocess
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from watchdog.config import load_config


DEPLOY_FILES = (
    (Path("watchdog/recovery.py"), "recovery.py"),
    (Path("watchdog/state_store.py"), "state_store.py"),
    (Path("watchdog/tests/test_recovery.py"), "test_recovery.py"),
)
STAGE_FILES = DEPLOY_FILES + ((Path("scripts/deploy_watchdog_prod.ps1"), "deploy_watchdog_prod.ps1"),)


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _encoded_command(script: str) -> str:
    return base64.b64encode(script.encode("utf-16le")).decode("ascii")


def _run_local_checks(repo_root: Path) -> None:
    subprocess.run(
        [sys.executable, "-m", "compileall", "scripts", "watchdog"],
        cwd=repo_root,
        check=True,
    )
    subprocess.run(
        [
            sys.executable,
            "-m",
            "unittest",
            "watchdog.tests.test_recovery",
            "watchdog.tests.test_telegram_notifier",
            "-v",
        ],
        cwd=repo_root,
        check=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Deploy the watchdog recovery/backoff fix to the Windows production checkout."
    )
    parser.add_argument("--config", default="")
    parser.add_argument("--host", default="", help="Optional SSH target override (user@host).")
    parser.add_argument("--remote-root", default="", help="Optional production checkout path override.")
    parser.add_argument("--dry-run", action="store_true", help="Preview the target and files without deploying.")
    args = parser.parse_args()

    repo_root = REPO_ROOT
    config_path = Path(args.config) if args.config else repo_root / "config.yaml"
    config = load_config(str(config_path))
    host = args.host or (
        f"{config.deploy_ssh_user}@{config.deploy_server_ip}" if config.deploy_server_ip else ""
    )
    remote_root = args.remote_root or config.deploy_remote_root
    if not host:
        raise RuntimeError(f"Set deploy_server_ip in {config_path} or pass --host.")
    if not remote_root:
        raise RuntimeError(f"Set deploy_remote_root in {config_path} or pass --remote-root.")
    ssh_user = host.rsplit("@", 1)[0] if "@" in host else ""
    if not re.fullmatch(r"[A-Za-z0-9._-]+", ssh_user):
        raise RuntimeError("Deployment target must use user@host so its remote staging path is unambiguous.")

    files = [repo_root / relative for relative, _ in DEPLOY_FILES]
    print(f"Target: {host}:{remote_root}")
    print("Files:")
    for path in files:
        print(f"  {path.relative_to(repo_root)}")
    print("  scripts/deploy_watchdog_prod.ps1 (remote deployment steps)")
    if args.dry_run:
        print("Dry run only. No files were transferred and production was not changed.")
        return 0

    _run_local_checks(repo_root)
    stage_id = uuid.uuid4().hex[:12]
    stage_dir = rf"C:\Users\{ssh_user}\AppData\Local\Temp\nt-watchdog-deploy-{stage_id}"
    stage_scp = stage_dir.replace("\\", "/")

    create_stage = f"New-Item -ItemType Directory -Force -Path {_ps_quote(stage_dir)} | Out-Null"
    subprocess.run(
        ["ssh", "-o", "BatchMode=yes", host, "powershell.exe", "-NoProfile", "-EncodedCommand", _encoded_command(create_stage)],
        check=True,
    )
    for local_path, remote_name in STAGE_FILES:
        subprocess.run(
            ["scp", "-o", "BatchMode=yes", str(repo_root / local_path), f"{host}:{stage_scp}/{remote_name}"],
            check=True,
        )

    remote_script_path = stage_dir + "\\deploy_watchdog_prod.ps1"
    remote_script = (
        "$ErrorActionPreference = 'Stop'; "
        f"& {_ps_quote(remote_script_path)} "
        f"-RemoteRoot {_ps_quote(remote_root)} -StageDir {_ps_quote(stage_dir)}"
    )
    subprocess.run(
        ["ssh", "-o", "BatchMode=yes", host, "powershell.exe", "-NoProfile", "-EncodedCommand", _encoded_command(remote_script)],
        check=True,
    )
    cleanup = f"Remove-Item -LiteralPath {_ps_quote(stage_dir)} -Recurse -Force -ErrorAction SilentlyContinue"
    subprocess.run(
        ["ssh", "-o", "BatchMode=yes", host, "powershell.exe", "-NoProfile", "-EncodedCommand", _encoded_command(cleanup)],
        check=False,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
