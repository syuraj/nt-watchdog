from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Optional

from .config import WatchdogConfig


# PowerShell helper — watches for NT Welcome/login dialog and fills credentials
# via UIAutomation (not SendKeys, which is flaky under focus races). Targets
# AutomationId 'tbUserName', 'passwordBox', 'btnLogin'. Falls back to SendKeys
# only if UIA can't find the fields.
_PS_LOGIN_SCRIPT = r"""
$user = $env:NT_LOGIN_USER
$pwd  = $env:NT_LOGIN_PWD
if (-not $user -or -not $pwd) { exit 2 }

Add-Type -AssemblyName UIAutomationClient  -ErrorAction SilentlyContinue
Add-Type -AssemblyName UIAutomationTypes   -ErrorAction SilentlyContinue

$auto = [System.Windows.Automation.AutomationElement]
$tree = [System.Windows.Automation.TreeScope]

# Wait up to 90s for an NT window whose title looks like the login dialog.
$deadline = (Get-Date).AddSeconds(90)
$proc = $null
while ((Get-Date) -lt $deadline) {
    $proc = Get-Process NinjaTrader -ErrorAction SilentlyContinue |
        Where-Object { $_.MainWindowHandle -ne 0 -and $_.MainWindowTitle -match 'log ?in|sign in|welcome' } |
        Select-Object -First 1
    if ($proc) { break }
    Start-Sleep -Milliseconds 500
}
if (-not $proc) { exit 1 }

$pidCond = New-Object System.Windows.Automation.PropertyCondition($auto::ProcessIdProperty, $proc.Id)
$win = $auto::RootElement.FindFirst($tree::Children, $pidCond)
if (-not $win) { exit 3 }

function FindById($root, $id) {
    $c = New-Object System.Windows.Automation.PropertyCondition($auto::AutomationIdProperty, $id)
    return $root.FindFirst($tree::Descendants, $c)
}

$userBox = FindById $win 'tbUserName'
$pwdBox  = FindById $win 'passwordBox'
$btn     = FindById $win 'btnLogin'
if (-not $userBox -or -not $pwdBox -or -not $btn) { exit 4 }

# Set username via ValuePattern (clean, no focus games).
$vp = [System.Windows.Automation.ValuePattern]
$userVp = $userBox.GetCurrentPattern($vp::Pattern)
$userVp.SetValue($user)
Start-Sleep -Milliseconds 150

# PasswordBox rejects ValuePattern — must focus + SendKeys into it.
# Escape SendKeys meta-chars so +^%~(){}[] in password don't do magic.
function Esc($s) { return ($s -replace '([+^%~(){}\[\]])', '{$1}') }
Add-Type -AssemblyName System.Windows.Forms | Out-Null
$pwdBox.SetFocus()
Start-Sleep -Milliseconds 250
[System.Windows.Forms.SendKeys]::SendWait((Esc $pwd))
Start-Sleep -Milliseconds 250

# Click Log In via InvokePattern.
$ip = [System.Windows.Automation.InvokePattern]
$btnIp = $btn.GetCurrentPattern($ip::Pattern)
$btnIp.Invoke()
"""


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

    def _spawn_login_helper(self) -> None:
        """Launch a detached PowerShell that watches for the NT login dialog and
        types nt_username / nt_password. No-op if either is empty."""
        user = (self.config.nt_username or "").strip()
        pwd = (self.config.nt_password or "").strip()
        if not user or not pwd:
            return
        env = os.environ.copy()
        env["NT_LOGIN_USER"] = user
        env["NT_LOGIN_PWD"] = pwd
        try:
            subprocess.Popen(
                ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", _PS_LOGIN_SCRIPT],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            pass

    def start(self) -> bool:
        try:
            exe = self.resolve_executable_path()
        except Exception:
            return False

        try:
            subprocess.Popen([exe], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            return False
        self._spawn_login_helper()
        return True

    def restart(self, startup_grace_sec: int) -> bool:
        if not self.stop():
            return False
        if not self.start():
            return False
        time.sleep(max(5, startup_grace_sec))
        return self.is_running()

