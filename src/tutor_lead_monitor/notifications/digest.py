from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from tutor_lead_monitor.config import BusinessConfig
from tutor_lead_monitor.domain.models import utc


def day_bounds(day: date, timezone: str) -> tuple[datetime, datetime]:
    zone = ZoneInfo(timezone)
    return (
        datetime.combine(day, datetime.min.time(), zone).astimezone(UTC),
        datetime.combine(day + timedelta(days=1), datetime.min.time(), zone).astimezone(UTC),
    )


def next_digest(now: datetime, config: BusinessConfig) -> datetime:
    instant = utc(now)
    zone = ZoneInfo(config.timezone)
    day = instant.astimezone(zone).date()
    target = datetime.combine(day, config.digest_time, zone).astimezone(UTC)
    if target <= instant:
        target = datetime.combine(day + timedelta(days=1), config.digest_time, zone).astimezone(UTC)
    return target
