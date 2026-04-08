"""
Usage API client.

GET https://api.anthropic.com/api/oauth/usage
Headers:
  Authorization: Bearer <accessToken>
  anthropic-beta: oauth-2025-04-20

Response:
{
  "five_hour":        { "utilization": 56, "resets_at": "2026-04-07T17:00:00+00:00" },
  "seven_day":        { "utilization": 41, "resets_at": "2026-04-09T04:00:00+00:00" },
  "seven_day_sonnet": { "utilization": 30, "resets_at": "2026-04-09T06:00:00+00:00" }
}
"""

from __future__ import annotations

import json
import time
import urllib.request
import urllib.error
from typing import Optional

from cap.types import UsageDimension, UsageSnapshot

USAGE_API_URL = "https://api.anthropic.com/api/oauth/usage"


def fetch_usage(access_token: str) -> Optional[UsageSnapshot]:
    req = urllib.request.Request(
        USAGE_API_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "anthropic-beta": "oauth-2025-04-20",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = json.loads(resp.read().decode())
    except Exception:
        return None

    def dim(key: str) -> UsageDimension:
        d = raw.get(key, {})
        return UsageDimension(
            utilization=round(d.get("utilization", 0)),
            resets_at=d.get("resets_at", ""),
        )

    return UsageSnapshot(
        five_hour=dim("five_hour"),
        seven_day=dim("seven_day"),
        seven_day_sonnet=dim("seven_day_sonnet"),
        fetched_at=time.time(),
    )
