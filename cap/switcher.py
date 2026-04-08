"""
Account switcher — symlink-based.

核心思路:
  ~/.claude/.credentials.json 是一个 symlink
  指向 ~/.cap/accounts/<active>/creds.json

切换 = 改 symlink 指向，其他文件（settings、MCP、skills）完全不变。

macOS: 额外同步 Keychain（Claude Code 优先读 Keychain）。
Linux: 纯 symlink 即可。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from cap.credentials import read_credentials, sync_active_to_keychain

CLAUDE_HOME = Path.home() / ".claude"
CRED_LINK = CLAUDE_HOME / ".credentials.json"


def get_active_account(accounts_dir: str) -> Optional[str]:
    """Return the name of the currently active account (symlink target)."""
    if not CRED_LINK.is_symlink():
        return None
    target = CRED_LINK.resolve()
    accounts_root = Path(accounts_dir).resolve()
    try:
        rel = target.relative_to(accounts_root)
        return rel.parts[0] if rel.parts else None
    except ValueError:
        return None


def switch_to(accounts_dir: str, name: str) -> str:
    """
    Switch active account by updating the symlink + syncing Keychain on macOS.

    Returns the account name on success.
    Raises FileNotFoundError if account doesn't exist.
    """
    acct_dir = str(Path(accounts_dir) / name)
    source = Path(acct_dir) / "creds.json"
    if not source.exists():
        raise FileNotFoundError(f"no creds.json in account '{name}'")

    CLAUDE_HOME.mkdir(parents=True, exist_ok=True)

    # Remove existing file/symlink
    if CRED_LINK.exists() or CRED_LINK.is_symlink():
        CRED_LINK.unlink()

    # Create symlink
    CRED_LINK.symlink_to(source.resolve())

    # macOS: sync to Keychain so Claude Code picks it up immediately
    creds = read_credentials(acct_dir)
    if creds:
        sync_active_to_keychain(creds)

    return name
