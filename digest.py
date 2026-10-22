"""Daily volunteer digest sender. Schedule send_daily_digest() once daily via cron."""
from __future__ import annotations

import logging
import os
import threading
from datetime import date, datetime, timezone
from html import escape

import requests
from dotenv import load_dotenv
from supabase import create_client
from supabase.lib.client_options import ClientOptions

from scraper import SearchError, scrape_events

load_dotenv()
logger = logging.getLogger("volunteer_digest")
_lock = threading.Lock()
_sent_dates: set[str] = set()


def _required(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def send_daily_digest() -> dict:
    """Send at most one digest per UTC date per process; returns counts and status."""
    today = datetime.now(timezone.utc).date()
    day_key = today.isoformat()
    # Hold the lock for the whole run: concurrent calls in this process cannot duplicate sends.
    with _lock:
        if day_key in _sent_dates:
            return {"ok": True, "date": day_key, "sent": 0, "skipped": 0, "already_sent": True}
        supabase = create_client(
            _required("SUPABASE_URL"),
            _required("SUPABASE_SERVICE_ROLE_KEY"),
            options=ClientOptions(auto_refresh_token=False, persist_session=False),
        )
        api_key = _required("RESEND_API_KEY")
        from_email = _required("RESEND_FROM_EMAIL")
        result = supabase.table("profiles").select(
            "user_id,target_email,city,zip_code,logged_hours,plan_type,email_verified"
        ).eq("email_verified", True).not_.is_("target_email", "null").execute()
        sent = 0
        skipped = 0
        failures = []
        for profile in (result.data or []):
            email = (profile.get("target_email") or "").strip()
            if not email:
                skipped += 1
                continue
            city = (profile.get("city") or "").strip()
            zip_code = (profile.get("zip_code") or "").strip()
            if not city and not zip_code:
                skipped += 1
                continue
            try:
                events = scrape_events(city, zip_code)
            except (SearchError, ValueError) as exc:
                logger.warning("Digest scrape failed for profile %s: %s", profile.get("user_id"), exc)
                failures.append(str(profile.get("user_id")))
                skipped += 1
                continue
            # The scraper returns flexible leads, not necessarily dated future events.
            # Preserve its order and cap the digest to five items.
            items = events[:5]
            event_html = "".join(
                "<li style='margin:0 0 14px'>"
                f"<strong>{escape(str(item.get('title') or 'Volunteer opportunity'))}</strong><br>"
                f"{escape(str(item.get('date') or 'Flexible / See Link'))} · "
                f"{escape(str(item.get('location') or 'Location not provided'))}<br>"
                f"<a href='{escape(str(item.get('link') or ''), quote=True)}'>View opportunity</a>"
                "</li>"
                for item in items
            )
            if not event_html:
                event_html = "<p>No matching opportunities were found today. Try checking again later.</p>"
            plan = profile.get("plan_type") or "free"
            if plan == "pro":
                footer = "Thanks for being Pro. Unlimited tracking enabled."
            else:
                footer = f"You are at {float(profile.get('logged_hours') or 0):g}/20 hours. Upgrade to Pro."
            html = (
                "<div style='font-family:Arial,sans-serif;color:#1c1a16;line-height:1.6'>"
                f"<h1>Your volunteer digest — {today.isoformat()}</h1>"
                "<p>Here are volunteer opportunities near your saved location.</p>"
                f"<ul>{event_html}</ul><hr><p>{escape(footer)}</p></div>"
            )
            try:
                response = requests.post(
                    "https://api.resend.com/emails",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={
                        "from": from_email,
                        "to": [email],
                        "subject": f"Your volunteer digest — {today.isoformat()}",
                        "html": html,
                    },
                    timeout=(10, 30),
                )
                response.raise_for_status()
                sent += 1
            except requests.RequestException:
                logger.exception("Resend failed for profile %s", profile.get("user_id"))
                failures.append(str(profile.get("user_id")))
                skipped += 1
        # Mark date only if all profiles were processed successfully. This prevents
        # duplicate sends during normal repeated calls, while permitting retry after errors.
        if not failures:
            _sent_dates.add(day_key)
        return {
            "ok": not failures,
            "date": day_key,
            "sent": sent,
            "skipped": skipped,
            "already_sent": False,
            "failed_profile_ids": failures,
        }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(send_daily_digest())
