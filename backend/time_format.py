from datetime import datetime, timezone
from zoneinfo import ZoneInfo

TORONTO = ZoneInfo("America/Toronto")


def _utc_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def compact_timestamp(value: str, now: str) -> str:
    """Age against the page's clock, then the Toronto date with the year when needed."""
    moment, current = _utc_datetime(value), _utc_datetime(now)
    seconds = (current - moment).total_seconds()
    if seconds < 60:
        return "now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    local = moment.astimezone(TORONTO)
    year = "" if local.year == current.astimezone(TORONTO).year else f", {local.year}"
    return f"{local.strftime('%b').lower()} {local.day}{year}"


def full_timestamp(value: str, zone: str) -> str:
    """Toronto wall time; zone is the serializer's EST or EDT for this instant."""
    return _utc_datetime(value).astimezone(TORONTO).strftime(f"%B %-d, %Y at %-I:%M %p {zone}")
