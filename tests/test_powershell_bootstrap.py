"""PowerShell resolves installation and agent configuration under the same profile."""

import os
from pathlib import Path
import shutil
import subprocess

import pytest


def test_powershell_uses_configured_user_profile(tmp_path):
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if not powershell:
        pytest.skip("PowerShell is not installed on this platform")
    installer = Path(__file__).resolve().parents[1] / "install/install.ps1"
    profile = tmp_path / "profile"
    profile.mkdir()
    env = dict(os.environ, USERPROFILE=str(profile))
    quoted = str(installer).replace("'", "''")
    driver = f"""
function global:Invoke-WebRequest {{
    Write-Output ('PROFILE_ROOT=' + $nodusRoot)
    throw 'Download intentionally stopped before installing anything'
}}
try {{ & '{quoted}' }}
catch {{
    if ($_.Exception.Message -ne 'Download intentionally stopped before installing anything') {{ throw }}
}}
exit 0
"""
    result = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", driver],
                            env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "PROFILE_ROOT=" + str(profile / ".nodus") in result.stdout
