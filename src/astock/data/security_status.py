"""Dated, evidenced exceptions to the expectation of a daily traded bar.

An empty vendor response is never a security status. Suspensions expire at the
last verified session; an effective resumption ends an earlier suspension, while
an already effective definitive delisting remains effective. Announcements
published after the requested session cannot affect a historical query.
"""
import json
import logging
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from .models import normalize_symbol

logger = logging.getLogger(__name__)


def load_nontrading_statuses(path: Path, as_of: str, symbols=None):
    requested = date.fromisoformat(as_of)
    scope = {normalize_symbol(s) for s in symbols} if symbols is not None else None
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise ValueError("unsupported_security_status_schema")
        events = []
        for entry in payload.get("records", []):
            try:
                symbol = normalize_symbol(entry["symbol"])
                status = entry["status"]
                if scope is not None and symbol not in scope:
                    continue
                if status not in ("SUSPENDED", "DELISTED", "RESUMED"):
                    continue
                announced = date.fromisoformat(entry["announcement_date"])
                effective = date.fromisoformat(entry["effective_from"])
                if max(announced, effective) > requested:
                    continue
                verified = date.fromisoformat(entry["verified_through"])
                if status == "SUSPENDED" and requested > verified:
                    continue
                host = (urlparse(entry["source_url"]).hostname or "").lower()
                if not any(host == h or host.endswith("." + h)
                           for h in ("cninfo.com.cn", "sse.com.cn", "szse.cn")):
                    continue
                if not entry.get("title") or not entry.get("evidence_summary"):
                    continue
                events.append(dict(entry, symbol=symbol))
            except (KeyError, TypeError, ValueError):
                logger.warning("Ignored malformed dated security-status evidence")
        # Process dated evidence chronologically, independently of file order.
        # A same-day resumption wins a same-day suspension when disclosure dates
        # are equal; a later suspension can establish a new halted interval.
        priority = {"SUSPENDED": 0, "RESUMED": 1, "DELISTED": 2}
        events.sort(key=lambda event: (event["effective_from"], event["announcement_date"],
                    priority[event["status"]], json.dumps(event, sort_keys=True, ensure_ascii=False)))
        result = {}
        for event in events:
            symbol, status = event["symbol"], event["status"]
            if result.get(symbol, {}).get("status") == "DELISTED":
                continue  # A resume event cannot resurrect a definitively delisted security.
            if status == "RESUMED":
                result.pop(symbol, None)
            else:
                result[symbol] = event
        return result
    except (OSError, TypeError, ValueError):
        logger.warning("Security-status evidence unavailable; retaining normal missing-data checks")
        return {}
