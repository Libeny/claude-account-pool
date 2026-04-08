"""
Credential file I/O.

Linux:  每个账号目录下的 creds.json
macOS:  同上（creds.json），Keychain 单独通过 sync_active_to_keychain 同步

重要: write_credentials 只写文件，不动 Keychain。
     Keychain 仅在 switch_to 或刷新活跃账号时更新。
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from cap.types import CredentialsFile

_KEYCHAIN_SERVICE = "Claude Code-credentials"


def _cred_path(config_dir: str) -> Path:
    return Path(config_dir) / "creds.json"


def _to_json(creds: CredentialsFile) -> str:
    return json.dumps({
        "claudeAiOauth": {
            "accessToken": creds.access_token,
            "refreshToken": creds.refresh_token,
            "expiresAt": creds.expires_at,
            "scopes": creds.scopes,
        }
    }, indent=2)


def _parse_oauth(raw: dict) -> Optional[CredentialsFile]:
    oauth = raw.get("claudeAiOauth", {})
    if not oauth.get("accessToken") or not oauth.get("refreshToken"):
        return None
    return CredentialsFile(
        access_token=oauth["accessToken"],
        refresh_token=oauth["refreshToken"],
        expires_at=int(oauth.get("expiresAt", 0)),
        scopes=oauth.get("scopes", []),
    )


# ── Read ──


def read_credentials(config_dir: str) -> Optional[CredentialsFile]:
    """Read credentials from the account's creds.json file."""
    p = _cred_path(config_dir)
    # Follow symlink if needed
    if p.is_symlink():
        p = p.resolve()
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text())
        return _parse_oauth(raw)
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


# ── Write (file only, never touches Keychain) ──


def write_credentials(config_dir: str, creds: CredentialsFile) -> None:
    """Write credentials to the account's creds.json file. Does NOT update Keychain."""
    p = _cred_path(config_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
    try:
        os.write(fd, _to_json(creds).encode())
        os.close(fd)
        os.chmod(tmp, 0o600)
        os.replace(tmp, str(p))
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# ── Keychain (macOS only, called explicitly for active account) ──


def sync_active_to_keychain(creds: CredentialsFile) -> None:
    """Update macOS Keychain with the active account's credentials.
    Only call this for the currently active (symlinked) account.
    No-op on Linux."""
    if platform.system() != "Darwin":
        return
    try:
        subprocess.run(
            [
                "security", "add-generic-password",
                "-U", "-s", _KEYCHAIN_SERVICE,
                "-a", "default", "-w", _to_json(creds),
            ],
            capture_output=True, timeout=5,
        )
    except Exception:
        pass


def read_keychain() -> Optional[CredentialsFile]:
    """Read credentials from macOS Keychain. Returns None on Linux or failure."""
    if platform.system() != "Darwin":
        return None
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", _KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return None
        raw = json.loads(result.stdout.strip())
        return _parse_oauth(raw)
    except Exception:
        return None
