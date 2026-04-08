"""CAP — Claude Account Pool."""

from cap.pool import AccountPool
from cap.credentials import read_credentials, write_credentials, sync_active_to_keychain
from cap.token_refresher import refresh_token
from cap.usage_monitor import fetch_usage
from cap.switcher import switch_to, get_active_account
from cap.account_meta import read_meta, write_meta, extract_meta_from_claude_json
from cap.types import AccountState, UsageSnapshot, CredentialsFile, AccountMeta

__all__ = [
    "AccountPool",
    "read_credentials",
    "write_credentials",
    "sync_active_to_keychain",
    "refresh_token",
    "fetch_usage",
    "switch_to",
    "get_active_account",
    "read_meta",
    "write_meta",
    "extract_meta_from_claude_json",
    "AccountState",
    "UsageSnapshot",
    "CredentialsFile",
    "AccountMeta",
]
