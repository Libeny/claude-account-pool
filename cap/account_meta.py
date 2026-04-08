"""
Account metadata: email, display name, org.

Stored as meta.json in each account directory.
Extracted from ~/.claude.json (oauthAccount) during account import.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from cap.types import AccountMeta

CLAUDE_JSON = Path.home() / ".claude.json"


def read_meta(config_dir: str) -> AccountMeta:
    p = Path(config_dir) / "meta.json"
    if not p.exists():
        return AccountMeta()
    try:
        d = json.loads(p.read_text())
        return AccountMeta(
            email=d.get("email", ""),
            display_name=d.get("display_name", ""),
            org_name=d.get("org_name", ""),
        )
    except Exception:
        return AccountMeta()


def write_meta(config_dir: str, meta: AccountMeta) -> None:
    p = Path(config_dir) / "meta.json"
    p.write_text(json.dumps({
        "email": meta.email,
        "display_name": meta.display_name,
        "org_name": meta.org_name,
    }, ensure_ascii=False, indent=2))


def extract_meta_from_claude_json(claude_json_path: Optional[Path] = None) -> AccountMeta:
    """Extract account metadata from ~/.claude.json oauthAccount."""
    p = claude_json_path or CLAUDE_JSON
    if not p.exists():
        return AccountMeta()
    try:
        d = json.loads(p.read_text())
        oa = d.get("oauthAccount", {})
        return AccountMeta(
            email=oa.get("emailAddress", ""),
            display_name=oa.get("displayName", ""),
            org_name=oa.get("organizationName", ""),
        )
    except Exception:
        return AccountMeta()


def email_to_dirname(email: str) -> str:
    """Convert email to a safe directory name: jjuuzhang26@gmail.com -> jjuuzhang26."""
    if not email:
        return "default"
    return email.split("@")[0].replace(".", "-").replace("+", "-")
