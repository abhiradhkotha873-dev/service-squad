"""Google Calendar draft-link utility. Never infers dates."""

from datetime import date, datetime, timezone
from urllib.parse import urlencode

DISCLAIMER = (
    "Unverified discovery lead. Confirm eligibility, location, and "
    "schedule directly with the organizer before attending."
)


def _require_aware_datetime(value, name):
    if not isinstance(value, datetime):
        raise ValueError(f"{name} must be a datetime, not {type(value).__name__}.")
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(f"{name} must be timezone-aware.")


def _require_date(value, name):
    if isinstance(value, datetime):
        raise ValueError(
            f"{name} must be a date, not a datetime. Use all_day_* for "
            "all-day events."
        )
    if not isinstance(value, date):
        raise ValueError(f"{name} must be a date.")


def _to_google_utc(dt):
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _to_google_date(d):
    return d.strftime("%Y%m%d")


def google_calendar_url(
    *, title, details="", location=None,
    start=None, end=None, all_day_start=None, all_day_end=None,
):
    if title is None or not str(title).strip():
        raise ValueError("title is required.")
    has_timed = start is not None or end is not None
    has_all_day = all_day_start is not None or all_day_end is not None
    if has_timed and has_all_day:
        raise ValueError("Cannot mix timed and all-day scheduling inputs.")
    params = {"action": "TEMPLATE", "text": str(title).strip()}
    full_details = str(details or "").strip()
    params["details"] = (
        full_details + "\n\n" + DISCLAIMER if full_details else DISCLAIMER
    )
    if location is not None:
        location_str = str(location).strip()
        if location_str:
            params["location"] = location_str
    if not has_timed and not has_all_day:
        return None
    if has_timed:
        if start is None or end is None:
            raise ValueError("Both start and end are required for timed events.")
        _require_aware_datetime(start, "start")
        _require_aware_datetime(end, "end")
        if end <= start:
            raise ValueError("end must be after start.")
        params["dates"] = f"{_to_google_utc(start)}/{_to_google_utc(end)}"
    else:
        if all_day_start is None or all_day_end is None:
            raise ValueError(
                "Both all_day_start and all_day_end are required for all-day events."
            )
        _require_date(all_day_start, "all_day_start")
        _require_date(all_day_end, "all_day_end")
        if all_day_end <= all_day_start:
            raise ValueError(
                "all_day_end must be after all_day_start (end is exclusive)."
            )
        params["dates"] = (
            f"{_to_google_date(all_day_start)}/{_to_google_date(all_day_end)}"
        )
    return "https://calendar.google.com/calendar/render?" + urlencode(params)
