from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def venv_python(root: Path) -> Path:
    return root / ".venv" / "Scripts" / "python.exe"


def run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(cmd, text=True, capture_output=True)
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"Command failed ({proc.returncode}): {' '.join(cmd)}\n"
            f"stdout:\n{proc.stdout}\n"
            f"stderr:\n{proc.stderr}"
        )
    return proc


def set_yaml_scalar(path: Path, key: str, value: str, quote: bool = True) -> None:
    if not path.exists():
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    prefix = f"{key}:"
    replaced = False
    rendered = f"'{value}'" if quote else value
    new_line = f"{key}: {rendered}"
    for idx, line in enumerate(lines):
        if line.strip().startswith(prefix):
            lines[idx] = new_line
            replaced = True
            break
    if not replaced:
        lines.append(new_line)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def ensure_venv(root: Path, python_exe: str) -> Path:
    py = venv_python(root)
    if py.exists():
        return py
    print(f"Creating venv at {root / '.venv'}")
    run([python_exe, "-m", "venv", str(root / ".venv")])
    return py


def setup(args: argparse.Namespace) -> None:
    root = project_root()
    py = ensure_venv(root, args.python)
    req = root / "watchdog" / "requirements.txt"
    cfg_example = root / "watchdog" / "config.yaml.example"
    cfg = root / "watchdog" / "config.yaml"

    print("Installing dependencies...")
    run([str(py), "-m", "pip", "install", "--upgrade", "pip"])
    run([str(py), "-m", "pip", "install", "-r", str(req)])

    if not cfg.exists():
        shutil.copy2(cfg_example, cfg)
        print(f"Created config: {cfg}")

    set_yaml_scalar(cfg, "bridge_url", args.bridge_url, quote=True)
    if args.nt_executable_path:
        set_yaml_scalar(cfg, "nt_executable_path", args.nt_executable_path, quote=True)

    # Copy HealthBridge.cs to NT AddOns folder so NinjaScript can compile it.
    bridge_src = root / "HealthBridge.cs"
    addons_dir = Path(os.path.expanduser("~")) / "Documents" / "NinjaTrader 8" / "bin" / "Custom" / "AddOns"
    if bridge_src.exists() and addons_dir.exists():
        bridge_dst = addons_dir / "HealthBridge.cs"
        shutil.copy2(bridge_src, bridge_dst)
        print(f"Copied bridge: {bridge_dst}")
        print("  -> open NinjaTrader > NinjaScript > Compile (F5) to load changes.")
    elif not addons_dir.exists():
        print(f"NT AddOns folder not found at {addons_dir}; skipping bridge copy.")

    print("\nSetup complete.")
    print(f"Run watchdog: {py} -m watchdog.monitor --config {cfg}")


def cmd_run(args: argparse.Namespace) -> None:
    root = project_root()
    py = venv_python(root)
    py_exe = str(py if py.exists() else Path(sys.executable))
    cfg = args.config if args.config else str(root / "watchdog" / "config.yaml")
    cmd = [py_exe, "-m", "watchdog.monitor", "--config", cfg]
    if args.max_cycles and args.max_cycles > 0:
        cmd += ["--max-cycles", str(args.max_cycles)]
    subprocess.run(cmd, check=True)


def cmd_smoke_test(args: argparse.Namespace) -> None:
    root = project_root()
    py = venv_python(root)
    py_exe = str(py if py.exists() else Path(sys.executable))
    mock_script = root / "watchdog" / "tests" / "mock_healthbridge.py"
    smoke_cfg = root / "watchdog" / "tests" / "smoke_config.yaml"
    events_path = root / "watchdog" / "logs" / "smoke_events.jsonl"
    snapshot_path = root / "watchdog" / "state" / "smoke_snapshot.json"

    smoke_cfg.write_text(
        "\n".join(
            [
                f'bridge_url: "http://127.0.0.1:{args.mock_port}"',
                "poll_interval_sec: 1",
                "startup_grace_sec: 999",
                "reconnect_attempt_limit: 2",
                "restart_cooldown_sec: 30",
                "max_restarts_per_hour: 1",
                f'snapshot_path: "{str(snapshot_path).replace("\\", "\\\\")}"',
                f'events_log_path: "{str(events_path).replace("\\", "\\\\")}"',
                "telegram_enabled: false",
                "",
            ]
        ),
        encoding="utf-8",
    )

    mock_proc: Optional[subprocess.Popen[str]] = None
    try:
        print("Starting mock HealthBridge...")
        mock_proc = subprocess.Popen(
            [py_exe, str(mock_script), "--port", str(args.mock_port), "--mode", args.mode],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(1)
        print("Running watchdog smoke test...")
        subprocess.run(
            [
                py_exe,
                "-m",
                "watchdog.monitor",
                "--config",
                str(smoke_cfg),
                "--max-cycles",
                str(args.max_cycles),
            ],
            check=True,
        )
    finally:
        if mock_proc is not None and mock_proc.poll() is None:
            mock_proc.terminate()
            try:
                mock_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                mock_proc.kill()

    if args.cleanup:
        for p in (smoke_cfg, events_path, snapshot_path):
            if p.exists():
                p.unlink()
    print("Smoke test completed.")


def startup_folder() -> Path:
    appdata = os.getenv("APPDATA")
    if not appdata:
        raise RuntimeError("APPDATA is not set; cannot resolve Startup folder.")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def startup_launcher_path(name: str) -> Path:
    safe = "".join(ch for ch in name if ch.isalnum() or ch in ("-", "_", " ")).strip()
    if not safe:
        safe = "NT8-Health-Watchdog"
    return startup_folder() / f"{safe}.cmd"


def cmd_install_startup(args: argparse.Namespace) -> None:
    root = project_root()
    startup = startup_folder()
    startup.mkdir(parents=True, exist_ok=True)

    py = venv_python(root)
    python_exe = Path(args.python) if args.python else py
    if not python_exe.exists():
        python_exe = py if py.exists() else Path(sys.executable)

    config_path = Path(args.config) if args.config else (root / "watchdog" / "config.yaml")
    launcher = startup_launcher_path(args.name)

    config_str = str(config_path)
    py_str = str(python_exe)
    arg_str = f'-m watchdog.monitor --config "{config_str}"'
    config_ps = config_str.replace("'", "''")
    py_ps = py_str.replace("'", "''")
    arg_ps = arg_str.replace("'", "''")
    ps_cmd = (
        f"$cfg='{config_ps}'; "
        "$existing = Get-CimInstance Win32_Process | Where-Object { "
        "$_.Name -match '^python(\\.exe)?$' -and "
        "$_.CommandLine -match 'watchdog\\.monitor' -and "
        "$_.CommandLine -match [regex]::Escape($cfg) }; "
        "if (-not $existing) { "
        f"Start-Process -WindowStyle Minimized -FilePath '{py_ps}' "
        f"-ArgumentList '{arg_ps}' "
        "}"
    )

    # Startup launcher prevents duplicate watchdog instances for same config.
    lines = [
        "@echo off",
        f'cd /d "{root}"',
        f'powershell -NoProfile -Command "{ps_cmd}"',
        "",
    ]
    launcher.write_text("\n".join(lines), encoding="utf-8")
    print(f"Startup launcher created: {launcher}")
    print("Watchdog will auto-start on user login.")


def cmd_remove_startup(args: argparse.Namespace) -> None:
    launcher = startup_launcher_path(args.name)
    if launcher.exists():
        launcher.unlink()
        print(f"Startup launcher removed: {launcher}")
    else:
        print(f"No startup launcher found: {launcher}")


def cmd_trigger_startup(args: argparse.Namespace) -> None:
    launcher = startup_launcher_path(args.name)
    if not launcher.exists():
        raise RuntimeError(f"Startup launcher not found: {launcher}. Run install-startup first.")
    # Run the launcher once right now.
    subprocess.run(["cmd", "/c", str(launcher)], check=True)
    print(f"Startup launcher triggered: {launcher}")


def _rdp_script(name: str) -> Path:
    path = project_root() / "scripts" / name
    if not path.exists():
        raise RuntimeError(f"script not found: {path}")
    return path


def cmd_install_rdp_handler(args: argparse.Namespace) -> None:
    script = _rdp_script("install-rdp-handler.ps1")
    target_user = args.user or os.environ.get("USERNAME", "")
    print(f"Installing RDP disconnect handler for user '{target_user}' (requires admin)...")
    subprocess.run(
        [
            "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
            "-TargetUser", target_user,
            "-TaskName", args.name,
        ],
        check=True,
    )


def cmd_remove_rdp_handler(args: argparse.Namespace) -> None:
    script = _rdp_script("remove-rdp-handler.ps1")
    subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "-TaskName", args.name],
        check=True,
    )


def cmd_trigger_rdp_handler(args: argparse.Namespace) -> None:
    script = _rdp_script("rdp_disconnect_handler.ps1")
    target_user = args.user or os.environ.get("USERNAME", "")
    print(f"Invoking RDP handler directly (dry run, target user '{target_user}')...")
    subprocess.run(
        [
            "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
            "-TargetUser", target_user,
        ],
        check=True,
    )


def _simple_yaml_lookup(path: Path, key: str) -> Optional[str]:
    if not path.exists():
        return None
    prefix = f"{key}:"
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not line.startswith(prefix):
            continue
        value = line[len(prefix):].strip()
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        return value
    return None


def load_status_settings(root: Path, config_path: Path) -> tuple[str, Path]:
    bridge_url = "http://localhost:8899"
    events_log = root / "watchdog" / "logs" / "health_events.jsonl"

    if config_path.exists():
        try:
            import yaml  # type: ignore

            loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            if isinstance(loaded, dict):
                bridge_url = str(loaded.get("bridge_url", bridge_url)).rstrip("/")
                events_raw = str(loaded.get("events_log_path", str(events_log)))
                events_log = Path(events_raw)
        except Exception:
            raw_bridge = _simple_yaml_lookup(config_path, "bridge_url")
            raw_events = _simple_yaml_lookup(config_path, "events_log_path")
            if raw_bridge:
                bridge_url = raw_bridge.rstrip("/")
            if raw_events:
                events_log = Path(raw_events)

    if not events_log.is_absolute():
        events_log = root / events_log
    return bridge_url, events_log


def check_bridge_health(bridge_url: str) -> tuple[bool, str, dict]:
    for endpoint in ("/healthz", "/health"):
        url = bridge_url.rstrip("/") + endpoint
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=4) as resp:
                body = resp.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(body)
                status = parsed.get("status", "ok")
                service = parsed.get("service", "unknown")
                return True, f"UP ({service}, status={status}, endpoint={endpoint})", parsed if isinstance(parsed, dict) else {}
            except Exception:
                return True, f"UP (endpoint={endpoint})", {}
        except Exception:
            continue
    return False, "DOWN (no response from /healthz or /health)", {}


def find_watchdog_processes() -> list[dict]:
    ps_cmd = (
        "$p = Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -match '^python(\\.exe)?$' -and "
        "$_.CommandLine -match 'watchdog\\.monitor' } | "
        "Select-Object ProcessId,Name,CommandLine; "
        "$p | ConvertTo-Json -Compress"
    )
    proc = run(["powershell", "-NoProfile", "-Command", ps_cmd], check=False)
    if proc.returncode != 0:
        return []
    out = (proc.stdout or "").strip()
    if not out or out == "null":
        return []
    try:
        parsed = json.loads(out)
    except json.JSONDecodeError:
        return []
    if isinstance(parsed, list):
        return [p for p in parsed if isinstance(p, dict)]
    if isinstance(parsed, dict):
        return [parsed]
    return []


def read_last_event_line(path: Path) -> str:
    if not path.exists():
        return ""
    last = ""
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if stripped:
                last = stripped
    return last


def cmd_status(args: argparse.Namespace) -> None:
    root = project_root()
    config_path = Path(args.config) if args.config else (root / "watchdog" / "config.yaml")
    bridge_url, events_log = load_status_settings(root, config_path)

    bridge_up, bridge_msg, bridge_payload = check_bridge_health(bridge_url)
    processes = find_watchdog_processes()
    startup_launcher = startup_launcher_path(args.name)
    startup_installed = startup_launcher.exists()

    events_exists = events_log.exists()
    events_size = events_log.stat().st_size if events_exists else 0
    events_mtime = (
        datetime.fromtimestamp(events_log.stat().st_mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
        if events_exists
        else "n/a"
    )
    last_event_raw = read_last_event_line(events_log)
    last_event = last_event_raw
    if len(last_event) > 180:
        last_event = last_event[:177] + "..."

    bridge_status = str(bridge_payload.get("status", "unknown"))
    conn_info = bridge_payload.get("connections", {}) if isinstance(bridge_payload, dict) else {}
    conn_total = conn_info.get("total") if isinstance(conn_info, dict) else None
    conn_connected = conn_info.get("connected") if isinstance(conn_info, dict) else None
    nt_connected = bool(isinstance(conn_connected, int) and conn_connected > 0)
    nt_connections_text = (
        f"{conn_connected}/{conn_total}"
        if isinstance(conn_connected, int) and isinstance(conn_total, int)
        else "unknown"
    )
    watchdog_running = len(processes) > 0

    last_event_time = "n/a"
    if last_event_raw:
        try:
            parsed_line = json.loads(last_event_raw)
            if isinstance(parsed_line, dict):
                last_event_time = str(parsed_line.get("time_utc", "n/a"))
        except Exception:
            pass

    if args.json:
        payload = {
            "bridge_url": bridge_url,
            "bridge_up": bridge_up,
            "bridge_message": bridge_msg,
            "bridge_status": bridge_status,
            "nt_connected": nt_connected,
            "nt_connections": {
                "connected": conn_connected,
                "total": conn_total,
                "display": nt_connections_text,
            },
            "watchdog_running": watchdog_running,
            "watchdog_process_count": len(processes),
            "watchdog_processes": processes,
            "startup_launcher_installed": startup_installed,
            "startup_launcher_path": str(startup_launcher),
            "events_log_path": str(events_log),
            "events_log_exists": events_exists,
            "events_log_size_bytes": events_size,
            "events_log_last_modified_utc": events_mtime,
            "events_log_last_event_time_utc": last_event_time,
            "events_log_last_line": last_event,
        }
        print(json.dumps(payload, indent=2))
        return

    print("NT8 Watchdog Status")
    print(f"- bridge_up: {'yes' if bridge_up else 'no'}")
    print(f"- bridge_status: {bridge_status}")
    print(f"- nt_connected: {'yes' if nt_connected else 'no'} ({nt_connections_text})")
    print(f"- watchdog_running: {'yes' if watchdog_running else 'no'} ({len(processes)} process)")
    print(f"- startup_on_login: {'enabled' if startup_installed else 'disabled'}")
    print(f"- last_event_utc: {last_event_time}")
    print(f"- logs: {events_log}")
    if args.verbose:
        print(f"- bridge_detail: {bridge_msg}")
        print(f"- bridge_url: {bridge_url}")
        print(f"- startup_launcher_path: {startup_launcher}")
        print(f"- events_log_size_bytes: {events_size}")
        print(f"- events_log_last_modified_utc: {events_mtime}")
        if last_event:
            print(f"- events_log_last_line: {last_event}")
    if processes and args.verbose:
        for p in processes:
            pid = p.get("ProcessId", "?")
            cmd = str(p.get("CommandLine", ""))
            if len(cmd) > 200:
                cmd = cmd[:197] + "..."
            print(f"  - pid={pid} cmd={cmd}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Python-first watchdog management CLI.")
    sub = parser.add_subparsers(dest="command", required=True)

    s_setup = sub.add_parser("setup", help="Create venv, install deps, create/update config.")
    s_setup.add_argument("--python", default=sys.executable, help="Python used to create venv.")
    s_setup.add_argument("--bridge-url", default="http://localhost:8899")
    s_setup.add_argument("--nt-executable-path", default="")
    s_setup.set_defaults(func=setup)

    s_run = sub.add_parser("run", help="Run watchdog monitor.")
    s_run.add_argument("--config", default="")
    s_run.add_argument("--max-cycles", type=int, default=0)
    s_run.set_defaults(func=cmd_run)

    s_smoke = sub.add_parser("smoke-test", help="Run mock bridge smoke test.")
    s_smoke.add_argument("--mock-port", type=int, default=18999)
    s_smoke.add_argument("--mode", choices=["ok", "degraded"], default="ok")
    s_smoke.add_argument("--max-cycles", type=int, default=3)
    s_smoke.add_argument("--cleanup", action="store_true")
    s_smoke.set_defaults(func=cmd_smoke_test)

    s_startup = sub.add_parser("install-startup", help="Auto-start watchdog on user login via Startup folder.")
    s_startup.add_argument("--name", default="NT8-Health-Watchdog")
    s_startup.add_argument("--config", default="")
    s_startup.add_argument("--python", default="")
    s_startup.set_defaults(func=cmd_install_startup)

    s_startup_rm = sub.add_parser("remove-startup", help="Remove Startup-folder auto-start launcher.")
    s_startup_rm.add_argument("--name", default="NT8-Health-Watchdog")
    s_startup_rm.set_defaults(func=cmd_remove_startup)

    s_startup_trigger = sub.add_parser("trigger-startup", help="Run Startup-folder launcher once without relogin.")
    s_startup_trigger.add_argument("--name", default="NT8-Health-Watchdog")
    s_startup_trigger.set_defaults(func=cmd_trigger_startup)

    s_rdp = sub.add_parser("install-rdp-handler", help="Register scheduled task that redirects disconnected RDP sessions to console (prevents NT8 chart freeze). Requires admin.")
    s_rdp.add_argument("--user", default="", help="Target RDP user (defaults to $USERNAME).")
    s_rdp.add_argument("--name", default="NT8-RDP-Redirect", help="Scheduled task name.")
    s_rdp.set_defaults(func=cmd_install_rdp_handler)

    s_rdp_rm = sub.add_parser("remove-rdp-handler", help="Unregister the RDP disconnect handler scheduled task.")
    s_rdp_rm.add_argument("--name", default="NT8-RDP-Redirect")
    s_rdp_rm.set_defaults(func=cmd_remove_rdp_handler)

    s_rdp_trig = sub.add_parser("trigger-rdp-handler-now", help="Invoke the RDP handler script directly (no scheduled task needed).")
    s_rdp_trig.add_argument("--user", default="")
    s_rdp_trig.set_defaults(func=cmd_trigger_rdp_handler)

    s_status = sub.add_parser("status", help="Show bridge/watchdog/startup status in one view.")
    s_status.add_argument("--config", default="")
    s_status.add_argument("--name", default="NT8-Health-Watchdog")
    s_status.add_argument("--verbose", action="store_true")
    s_status.add_argument("--json", action="store_true")
    s_status.set_defaults(func=cmd_status)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("Interrupted by user.", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()

