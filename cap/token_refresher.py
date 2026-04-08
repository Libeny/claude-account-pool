"""
OAuth token refresh.

Endpoint: POST https://platform.claude.com/v1/oauth/token
Body:     { grant_type: "refresh_token", refresh_token, client_id }

Returns new accessToken + expiresAt, may rotate refreshToken.
"""

from __future__ import annotations

import json
import time
import urllib.request
import urllib.error
from dataclasses import dataclass
from typing import Optional, Union

from cap.types import CredentialsFile

OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"


@dataclass
class RefreshOk:
    credentials: CredentialsFile


@dataclass
class RefreshFail:
    error: str


RefreshResult = Union[RefreshOk, RefreshFail]


def refresh_token(creds: CredentialsFile) -> RefreshResult:
    if not creds.refresh_token:
        return RefreshFail("no refresh_token")

    body = json.dumps({
        "grant_type": "refresh_token",
        "refresh_token": creds.refresh_token,
        "client_id": OAUTH_CLIENT_ID,
    }).encode()

    req = urllib.request.Request(
        OAUTH_TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        resp_body = e.read().decode(errors="replace")[:300] if hasattr(e, "read") else ""
        return RefreshFail(f"HTTP {e.code}: {resp_body}")
    except Exception as e:
        return RefreshFail(str(e))

    now_ms = int(time.time() * 1000)
    return RefreshOk(
        credentials=CredentialsFile(
            access_token=data["access_token"],
            expires_at=now_ms + data["expires_in"] * 1000,
            refresh_token=data.get("refresh_token", creds.refresh_token),
            scopes=data["scope"].split() if data.get("scope") else creds.scopes,
        )
    )
