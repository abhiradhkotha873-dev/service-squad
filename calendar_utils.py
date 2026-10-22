"""Google Calendar URL utility used by app.py."""
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import urlencode


def _parse_date(date_str: str) -> date:
    """Parse common display dates; unparseable labels fall back to today's UTC date."""
    value = " ".join(str(date_str or "").strip().split())
    if not value or value.casefold() == "flexible / see link":
        return datetime.now(timezone.utc).date()
    candidates = (
        "%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%m-%d-%Y",
        "%B %d, %Y", "%b %d, %Y", "%A, %B %d, %Y",
        "%a, %b %d, %Y", "%d %B %Y", "%d %b %Y",
    )
    for fmt in candidates:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    # Avoid silently parsing a yearless date into an arbitrary year.
    return datetime.now(timezone.utc).date()


def generate_google_calendar_link(title, date_str, location, details) -> str:
    """Return an all-day Google Calendar template URL; unknown dates use today's UTC date."""
    event_date = _parse_date(date_str)
    start = datetime.combine(event_date, time.min, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    params = {
        "action": "TEMPLATE",
        "text": str(title or "Volunteer opportunity")[:300],
        "dates": f"{start:%Y%m%d}/{end:%Y%m%d}",
        "details": str(details or "")[:4000],
        "location": str(location or "")[:1000],
        "ctz": "UTC",
    }
    return "https://calendar.google.com/calendar/render?" + urlencode(params)
