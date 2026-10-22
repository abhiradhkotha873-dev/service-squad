from datetime import date, datetime, timezone

import pytest

from app.calendar_utils import google_calendar_url


def test_no_schedule_returns_none():
    assert google_calendar_url(title="X") is None


def test_timed_url_encoding():
    url = google_calendar_url(
        title="Café ☕ Volunteers",
        details="Bring snacks",
        location="123 Main St",
        start=datetime(2025, 6, 1, 9, 0, tzinfo=timezone.utc),
        end=datetime(2025, 6, 1, 11, 0, tzinfo=timezone.utc),
    )
    assert url is not None
    assert "action=TEMPLATE" in url
    assert "dates=20250601T090000Z%2F20250601T110000Z" in url
    assert "Caf%C3%A9" in url


def test_naive_datetime_rejected():
    with pytest.raises(ValueError):
        google_calendar_url(
            title="X",
            start=datetime(2025, 6, 1, 9, 0),
            end=datetime(2025, 6, 1, 11, 0),
        )


def test_only_one_endpoint_rejected():
    with pytest.raises(ValueError):
        google_calendar_url(
            title="X",
            start=datetime(2025, 6, 1, 9, 0, tzinfo=timezone.utc),
        )


def test_end_before_start_rejected():
    with pytest.raises(ValueError):
        google_calendar_url(
            title="X",
            start=datetime(2025, 6, 1, 11, 0, tzinfo=timezone.utc),
            end=datetime(2025, 6, 1, 9, 0, tzinfo=timezone.utc),
        )


def test_all_day_exclusive_end():
    url = google_calendar_url(
        title="X",
        all_day_start=date(2025, 6, 1),
        all_day_end=date(2025, 6, 2),
    )
    assert "dates=20250601%2F20250602" in url


def test_all_day_datetime_rejected():
    with pytest.raises(ValueError):
        google_calendar_url(
            title="X",
            all_day_start=datetime(2025, 6, 1, tzinfo=timezone.utc),
            all_day_end=date(2025, 6, 2),
        )


def test_conflicting_inputs_rejected():
    with pytest.raises(ValueError):
        google_calendar_url(
            title="X",
            start=datetime(2025, 6, 1, 9, 0, tzinfo=timezone.utc),
            end=datetime(2025, 6, 1, 11, 0, tzinfo=timezone.utc),
            all_day_start=date(2025, 6, 1),
            all_day_end=date(2025, 6, 2),
        )
