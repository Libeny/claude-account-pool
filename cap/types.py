"""Core data types for CAP."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class AccountStatus(str, Enum):
    ACTIVE = "active"
    NEEDS_LOGIN = "needs_login"
    DISABLED = "disabled"


@dataclass
class CredentialsFile:
    """Raw structure of .credentials.json."""

    access_token: str
    refresh_token: str
    expires_at: int  # ms epoch
    scopes: list[str] = field(default_factory=list)


@dataclass
class AccountMeta:
    """Account metadata (stored as meta.json alongside creds.json)."""

    email: str = ""
    display_name: str = ""  # user-editable alias
    org_name: str = ""


@dataclass
class UsageDimension:
    """One usage dimension from the API."""

    utilization: int  # 0-100
    resets_at: str  # ISO 8601


@dataclass
class UsageSnapshot:
    """Full usage snapshot from /api/oauth/usage."""

    five_hour: UsageDimension
    seven_day: UsageDimension
    seven_day_sonnet: UsageDimension
    fetched_at: float  # time.time()


@dataclass
class AccountState:
    """Runtime state for one account."""

    name: str
    config_dir: str  # absolute path
    status: AccountStatus = AccountStatus.ACTIVE
    token_expires_at: int = 0
    last_refresh_at: Optional[float] = None
    usage: Optional[UsageSnapshot] = None
    previous_usage: Optional[UsageSnapshot] = None
    meta: AccountMeta = field(default_factory=AccountMeta)
