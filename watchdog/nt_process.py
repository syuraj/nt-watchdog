from __future__ import annotations

import shlex
import subprocess
import time
from pathlib import Path
from typing import Optional

from .config import WatchdogConfig


class NTProcessManager:
    def __init__(self, config: WatchdogConfig) -> None:
        self.config = config

    def _tasklist(self) -> str:
        out = subprocess.check_output(["tasklist", "/FI", f"IMAGENAME eq {self.config.nt_process_name}.exe"], text=True)
        return out

    def is_running(self) -> bool:
        try:
            out = self._tasklist()
        except Exception:
            return False
        return f"{self.config.nt_process_name}.exe" in out

    def detect_running_executable_path(self) -> Optional[str]:
        query = (
            "Get-CimInstance Win32_Process | "
            f"Where-Object {{$_.Name -eq '{self.config.nt_process_name}.exe'}} | "
            "Select-Object -First 1 -ExpandProperty ExecutablePath"
        )
        try:
            out = subprocess.check_output(["powershell", "-NoProfile", "-Command", query], text=True)
            path = out.strip()
            return path or None
        except Exception:
            return None

    def resolve_executable_path(self) -> str:
        configured = self.config.nt_executable_path
        if configured and Path(configured).exists():
            return configured
        if self.config.process_detect_fallback:
            detected = self.detect_running_executable_path()
            if detected and Path(detected).exists():
                return detected
        raise FileNotFoundError(
            f"NinjaTrader executable not found. Checked configured path: {configured}"
        )

    def stop(self, timeout_sec: int = 30) -> bool:
        if not self.is_running():
            return True
        # taskkill /T /F often returns non-zero when some children already exited.
        # Ignore its exit code and poll is_running() instead — that's the actual signal.
        subprocess.run(
            ["taskkill", "/IM", f"{self.config.nt_process_name}.exe", "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            if not self.is_running():
                return True
            time.sleep(1)
        return False

    def _build_start_args(self, exe: str) -> list:
        args = [exe] + list(self.config.nt_start_args or [])
        user = (self.config.nt_username or "").strip()
        pwd = (self.config.nt_password or "").strip()
        if user:
            args += ["-u", user]
        if pwd:
            args += ["-p", pwd]
        return args

    def start(self) -> bool:
        try:
            exe = self.resolve_executable_path()
        except Exception:
            return False

        args = self._build_start_args(exe)
        try:
            subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except Exception:
            return False

    def restart(self, startup_grace_sec: int) -> bool:
        if not self.stop():
            return False
        if not self.start():
            return False
        time.sleep(max(5, startup_grace_sec))
        return self.is_running()

