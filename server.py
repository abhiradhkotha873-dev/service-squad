"""
Volunteer-tracking SaaS API.
Run locally: flask --app app run
Production: gunicorn --workers 3 --threads 4 --timeout 90 app:app

All protected routes require a valid Supabase access token and a confirmed email.
Database access uses the service-role key only on the server; never expose it to clients.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import re
import traceback
from datetime import datetime, timezone
from functools import wraps
from urllib.parse import urlparse

import stripe
from dotenv import load_dotenv
from flask import Flask, g, jsonify, request
from flask_cors import CORS
from supabase import create_client
from supabase.lib.client_options import ClientOptions

from scraper import SearchError, scrape_events
from calendar_utils import generate_google_calendar_link

load_dotenv()

REQUIRED_ENV = (
    "SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_ROLE_KEY",
    "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "STRIPE_PRICE_ID",
    "RESEND_API_KEY", "RESEND_FROM_EMAIL", "FLASK_SECRET_KEY", "APP_BASE_URL",
)
_missing = [key for key in REQUIRED_ENV if not os.getenv(key, "").strip()]
if _missing:
    raise RuntimeError("Missing required environment variables: " + ", ".join(_missing))

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_ANON_KEY = os.environ["SUPABASE_ANON_KEY"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
APP_BASE_URL = os.environ["APP_BASE_URL"].rstrip("/")
if urlparse(APP_BASE_URL).scheme not in {"https", "http"} or not urlparse(APP_BASE_URL).netloc:
    raise RuntimeError("APP_BASE_URL must be an absolute http(s) URL.")
if urlparse(APP_BASE_URL).path not in {"", "/"} or urlparse(APP_BASE_URL).query or urlparse(APP_BASE_URL).fragment:
    raise RuntimeError("APP_BASE_URL must be an origin, e.g. https://example.com, without a path.")
stripe.api_key = os.environ["STRIPE_SECRET_KEY"]

class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)

_handler = logging.StreamHandler()
_handler.setFormatter(JsonFormatter())
root = logging.getLogger()
root.handlers.clear()
root.addHandler(_handler)
root.setLevel(logging.INFO)
logger = logging.getLogger("volunteer_api")

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ["FLASK_SECRET_KEY"]
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024
CORS(app, origins="*")

# A service-role client is only used server-side. Do not return it or its key.
db = create_client(
    SUPABASE_URL,
    SUPABASE_SERVICE_ROLE_KEY,
    options=ClientOptions(auto_refresh_token=False, persist_session=False)
# Admin client strictly for bypassing RLS
admin_db = create_client(
    SUPABASE_URL,
    SUPABASE_SERVICE_ROLE_KEY,
    options=ClientOptions(auto_refresh_token=False, persist_session=False)
)
,
)
auth_client = create_client(
    SUPABASE_URL,
    SUPABASE_ANON_KEY,
    options=ClientOptions(auto_refresh_token=False, persist_session=False),
)

EVENT_KEYS = {"city", "date", "event_hash", "link", "location", "title"}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
ZIP_RE = re.compile(r"^\d{5}$")


def error_response(code: str, message: str, status: int):
    return jsonify({"ok": False, "error": code, "message": message}), status


def json_body():
    if not request.is_json:
        return None
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else None


def safe_profile(row: dict) -> dict:
    """Public profile subset; deliberately excludes Stripe/subscription internals."""
    return {
        "username": row.get("username"),
        "target_email": row.get("target_email"),
        "logged_hours": float(row.get("logged_hours") or 0),
        "city": row.get("city") or "",
        "zip_code": row.get("zip_code") or "",
        "email_verified": bool(row.get("email_verified")),
        "plan_type": row.get("plan_type") or "free",
    }


def get_profile(user_id: str) -> dict | None:
    result = db.table("profiles").select(
        "user_id,username,target_email,logged_hours,city,zip_code,email_verified,"
        "plan_type,subscription_status,stripe_customer_id,subscription_end"
    ).eq("user_id", user_id).maybe_single().execute()
    return result.data if isinstance(result.data, dict) else None


def require_auth(fn):
    """Decorator verifies the bearer JWT with Supabase and requires confirmed email."""
    @wraps(fn)
    def wrapped(*args, **kwargs):
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return error_response("Unauthorized", "A Supabase access token is required.", 401)
        token = header[7:].strip()
        if not token:
            return error_response("Unauthorized", "A Supabase access token is required.", 401)
        try:
            # get_user(token) asks Supabase Auth to validate the token, rather than
            # trusting unverified JWT claims supplied by the client.
            result = auth_client.auth.get_user(token)
            user = getattr(result, "user", None)
            if user is None:
                return error_response("Unauthorized", "The access token is invalid or expired.", 401)
            confirmed_at = getattr(user, "email_confirmed_at", None)
            if not confirmed_at:
                return error_response("EmailNotVerified", "Confirm your email before using the app.", 401)
            user_id = str(user.id)
            profile = get_profile(user_id)
            if profile is None:
                # Supports projects where a profile trigger has not yet created the row.
                email = getattr(user, "email", None)
                inserted = db.table("profiles").upsert({
                    "user_id": user_id,
                    "target_email": email,
                    "email_verified": True,
                    "logged_hours": 0,
                    "plan_type": "free",
                    "subscription_status": "inactive",
                }, on_conflict="user_id").execute()
                profile = get_profile(user_id)
                if profile is None:
                    logger.error("Profile could not be loaded/created for authenticated user")
                    return error_response("ProfileUnavailable", "Your profile could not be loaded.", 500)
            elif not profile.get("email_verified"):
                db.table("profiles").update({"email_verified": True}).eq("user_id", user_id).execute()
                profile["email_verified"] = True
            g.user = user
            g.user_id = user_id
            g.profile = profile
            return fn(*args, **kwargs)
        except Exception:
            logger.exception("Authentication or profile lookup failed")
            return error_response("AuthServiceError", "Authentication could not be completed. Try again.", 503)
    return wrapped


@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Cache-Control"] = "no-store" if request.path.startswith("/api/") else "no-cache"
    return response


@app.errorhandler(413)
def too_large(_exc):
    return error_response("RequestTooLarge", "Request body exceeds the 64 KB limit.", 413)


@app.errorhandler(404)
def not_found(_exc):
    return error_response("NotFound", "The requested endpoint does not exist.", 404)


@app.errorhandler(405)
def method_not_allowed(_exc):
    return error_response("MethodNotAllowed", "This HTTP method is not supported for this endpoint.", 405)


@app.errorhandler(Exception)
def unhandled_error(exc):
    logger.exception("Unhandled request error")
    return error_response("InternalServerError", "An unexpected server error occurred.", 500)


@app.get("/health")
def health():
    """GET /health — no auth; body: none; response: {"ok":true,"status":"healthy"}."""
    return jsonify({"ok": True, "status": "healthy"})


@app.get("/api/events")
@require_auth
def events():
    """GET /api/events — JWT + confirmed email; body: none; returns six-key event dicts."""
    city = (g.profile.get("city") or "").strip()
    zip_code = (g.profile.get("zip_code") or "").strip()
    if not city and not zip_code:
        return error_response("ProfileLocationRequired", "Add a city or ZIP code in settings first.", 400)
    try:
        found = scrape_events(city, zip_code)
    except SearchError as exc:
        logger.warning("Scraper failed: %s", str(exc))
        return error_response("ScraperError", str(exc) or "Event search failed.", 502)
    except ValueError as exc:
        return error_response("InvalidLocation", str(exc), 400)
    except Exception:
        logger.exception("Unexpected scraper failure")
        return error_response("ScraperError", "Event search failed unexpectedly.", 502)

    # Enforce the Phase 1 contract and only show events for the caller's configured city.
    normalized_city = " ".join(city.casefold().split())
    filtered = []
    for event in found:
        if not isinstance(event, dict) or set(event.keys()) != EVENT_KEYS:
            logger.warning("Discarding scraper item that violates the six-key contract")
            continue
        event_city = " ".join(str(event.get("city") or "").casefold().split())
        if normalized_city and event_city != normalized_city:
            continue
        filtered.append(event)
    try:
        for event in filtered:
            db.table("events").upsert(event, on_conflict="event_hash").execute()
    except Exception:
        logger.exception("Could not persist discovered events")
        return error_response("DatabaseError", "Events were found but could not be saved. Try again.", 503)
    return jsonify({"ok": True, "events": filtered})


@app.post("/api/attend")
@require_auth
def attend():
    """POST /api/attend — JWT + confirmed email; body {"event_hash":"<str>"}; returns calendar_url."""
    data = json_body()
    if data is None:
        return error_response("InvalidJSON", "Send a JSON object.", 400)
    event_hash = data.get("event_hash")
    if not isinstance(event_hash, str) or not event_hash.strip() or len(event_hash) > 128:
        return error_response("InvalidEventHash", "event_hash must be a non-empty string.", 400)
    try:
        result = db.table("events").select(
            "event_hash,title,date,location,city,link"
        ).eq("event_hash", event_hash.strip()).maybe_single().execute()
        event = result.data if isinstance(result.data, dict) else None
        if not event:
            return error_response("EventNotFound", "This event was not found. Refresh the events list.", 404)
        calendar_url = generate_google_calendar_link(
            title=event.get("title") or "Volunteer opportunity",
            date_str=event.get("date") or "",
            location=event.get("location") or "",
            details=event.get("link") or "",
        )
        db.table("user_events").upsert({
            "user_id": g.user_id,
            "event_hash": event["event_hash"],
            "status": "Attending",
            "calendar_sync_url": calendar_url,
        }, on_conflict="user_id,event_hash").execute()
        return jsonify({"ok": True, "calendar_url": calendar_url})
    except Exception:
        logger.exception("Could not mark event as attending")
        return error_response("DatabaseError", "Could not save this event. Try again.", 503)


@app.post("/api/log-hours")
@require_auth
def log_hours():
    """POST /api/log-hours — JWT + confirmed email; body {"hours":number>0}; updates logged_hours."""
    data = json_body()
    if data is None:
        return error_response("InvalidJSON", "Send a JSON object.", 400)
    hours = data.get("hours")
    if isinstance(hours, bool) or not isinstance(hours, (int, float)):
        return error_response("InvalidHours", "hours must be a number greater than zero.", 400)
    hours = float(hours)
    if hours <= 0 or hours > 24 or hours != hours:
        return error_response("InvalidHours", "hours must be greater than 0 and no more than 24 per entry.", 400)
    try:
        current = get_profile(g.user_id)
        if not current:
            return error_response("ProfileUnavailable", "Your profile could not be loaded.", 500)
        logged = float(current.get("logged_hours") or 0)
        plan = current.get("plan_type") or "free"
        if plan == "free" and logged + hours > 20:
            return jsonify({
                "error": "PaywallTriggered",
                "message": "20-hour free limit reached. Upgrade to Pro for $4.99/month for unlimited tracking and PDF export.",
            }), 403
        new_total = round(logged + hours, 2)
        db.table("profiles").update({"logged_hours": new_total}).eq("user_id", g.user_id).execute()
        return jsonify({"ok": True, "logged_hours": new_total})
    except Exception:
        logger.exception("Could not log volunteer hours")
        return error_response("DatabaseError", "Hours could not be saved. Try again.", 503)


@app.post("/api/settings")
@require_auth
def settings():
    """POST /api/settings — JWT + confirmed email; body accepts city, zip_code, target_email."""
    data = json_body()
    if data is None:
        return error_response("InvalidJSON", "Send a JSON object.", 400)
    allowed = {"city", "zip_code", "target_email"}
    if not data or set(data) - allowed:
        return error_response("InvalidSettings", "Provide one or more of city, zip_code, target_email only.", 400)
    updates = {}
    for key in allowed & set(data):
        value = data[key]
        if not isinstance(value, str):
            return error_response("InvalidSettings", f"{key} must be a string.", 400)
        value = " ".join(value.split()).strip()
        if key == "city" and len(value) > 200:
            return error_response("InvalidCity", "City must be 200 characters or fewer.", 400)
        if key == "zip_code" and value and not ZIP_RE.fullmatch(value):
            return error_response("InvalidZipCode", "zip_code must be a 5-digit US ZIP code.", 400)
        if key == "target_email" and value and (len(value) > 254 or not EMAIL_RE.fullmatch(value)):
            return error_response("InvalidEmail", "target_email must be a valid email address.", 400)
        updates[key] = value or None if key == "target_email" else value
    if "city" in updates and "zip_code" in updates:
        if not updates["city"] and not updates["zip_code"]:
            return error_response("InvalidLocation", "Provide a city or ZIP code.", 400)
    try:
        db.table("profiles").update(updates).eq("user_id", g.user_id).execute()
        updated = get_profile(g.user_id)
        if not updated:
            return error_response("ProfileUnavailable", "Your profile could not be loaded.", 500)
        return jsonify({"ok": True, "profile": safe_profile(updated)})
    except Exception:
        logger.exception("Could not update profile settings")
        return error_response("DatabaseError", "Settings could not be saved. Try again.", 503)


@app.post("/api/create-checkout-session")
@require_auth
def create_checkout_session():
    """POST /api/create-checkout-session — JWT + confirmed email; body {}; returns Stripe Checkout URL."""
    data = json_body()
    if data is None:
        return error_response("InvalidJSON", "Send a JSON object (use {}).", 400)
    try:
        profile = get_profile(g.user_id)
        if not profile:
            return error_response("ProfileUnavailable", "Your profile could not be loaded.", 500)
        customer_id = profile.get("stripe_customer_id")
        if customer_id:
            try:
                customer = stripe.Customer.retrieve(customer_id)
                if getattr(customer, "deleted", False):
                    customer_id = None
            except stripe.error.InvalidRequestError:
                customer_id = None
        if not customer_id:
            email = getattr(g.user, "email", None) or profile.get("target_email")
            customer = stripe.Customer.create(
                email=email,
                metadata={"supabase_user_id": g.user_id},
            )
            customer_id = customer.id
            db.table("profiles").update({"stripe_customer_id": customer_id}).eq(
                "user_id", g.user_id
            ).execute()
        base_success = APP_BASE_URL + "/settings.html?checkout=success"
        base_cancel = APP_BASE_URL + "/settings.html?checkout=cancelled"
        session = stripe.checkout.Session.create(
            mode="subscription",
            customer=customer_id,
            line_items=[{"price": os.environ["STRIPE_PRICE_ID"], "quantity": 1}],
            success_url=base_success,
            cancel_url=base_cancel,
            client_reference_id=g.user_id,
            metadata={"supabase_user_id": g.user_id},
            subscription_data={"metadata": {"supabase_user_id": g.user_id}},
            allow_promotion_codes=True,
        )
        if not session.url:
            return error_response("StripeError", "Stripe did not return a checkout URL.", 502)
        return jsonify({"ok": True, "url": session.url})
    except stripe.error.StripeError:
        logger.exception("Stripe checkout creation failed")
        return error_response("StripeError", "Checkout could not be created. Try again.", 502)
    except Exception:
        logger.exception("Checkout session creation failed")
        return error_response("CheckoutError", "Checkout could not be created. Try again.", 502)


def _user_id_for_customer(customer_id: str, subscription=None, session=None) -> str | None:
    """Resolve subscription ownership using Stripe metadata first, then stored customer ID."""
    metadata = getattr(subscription, "metadata", None) if subscription is not None else None
    if metadata:
        value = metadata.get("supabase_user_id")
        if value:
            return str(value)
    metadata = getattr(session, "metadata", None) if session is not None else None
    if metadata:
        value = metadata.get("supabase_user_id")
        if value:
            return str(value)
    result = db.table("profiles").select("user_id").eq(
        "stripe_customer_id", customer_id
    ).maybe_single().execute()
    row = result.data if isinstance(result.data, dict) else None
    return str(row["user_id"]) if row and row.get("user_id") else None


def _subscription_end(subscription) -> str | None:
    timestamp = getattr(subscription, "current_period_end", None)
    if timestamp is None and isinstance(subscription, dict):
        timestamp = subscription.get("current_period_end")
    if not timestamp:
        return None
    return datetime.fromtimestamp(int(timestamp), tz=timezone.utc).isoformat()


def _sync_subscription(subscription):
    customer_id = str(getattr(subscription, "customer", "") or "")
    status = str(getattr(subscription, "status", "inactive") or "inactive")
    user_id = _user_id_for_customer(customer_id, subscription=subscription)
    if not user_id:
        logger.error("Stripe subscription has no resolvable Supabase user")
        return
    active = status in {"active", "trialing"}
    past_due = status in {"past_due", "unpaid", "incomplete"}
    db.table("profiles").update({
        "plan_type": "pro" if active else "free",
        "subscription_status": "active" if active else ("past_due" if past_due else "inactive"),
        "subscription_end": _subscription_end(subscription),
    }).eq("user_id", user_id).execute()


@app.post("/api/webhook/stripe")
def stripe_webhook():
    """POST /api/webhook/stripe — no JWT; raw Stripe payload + Stripe-Signature; returns received."""
    payload = request.get_data(cache=False)
    signature = request.headers.get("Stripe-Signature", "")
    try:
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=signature,
            secret=os.environ["STRIPE_WEBHOOK_SECRET"],
        )
    except (ValueError, stripe.error.SignatureVerificationError):
        return error_response("InvalidStripeSignature", "Invalid Stripe webhook signature or payload.", 400)
    try:
        event_type = event["type"]
        obj = event["data"]["object"]
        if event_type == "checkout.session.completed":
            if obj.get("mode") == "subscription" and obj.get("subscription") and obj.get("customer"):
                subscription = stripe.Subscription.retrieve(obj["subscription"])
                user_id = _user_id_for_customer(
                    str(obj["customer"]), subscription=subscription, session=obj
                )
                if user_id:
                    _sync_subscription(subscription)
                else:
                    logger.error("Completed Checkout session could not be linked to a user")
        elif event_type == "customer.subscription.updated":
            subscription = stripe.Subscription.retrieve(obj["id"])
            _sync_subscription(subscription)
        elif event_type == "customer.subscription.deleted":
            customer_id = str(obj.get("customer") or "")
            user_id = _user_id_for_customer(customer_id)
            if user_id:
                db.table("profiles").update({
                    "plan_type": "free",
                    "subscription_status": "inactive",
                    "subscription_end": _subscription_end(obj),
                }).eq("user_id", user_id).execute()
        # Other valid Stripe event types are acknowledged and ignored.
        return jsonify({"received": True}), 200
    except Exception:
        logger.exception("Stripe webhook processing failed")
        # A non-2xx response asks Stripe to retry transient processing failures.
        return error_response("WebhookProcessingError", "Webhook processing failed; Stripe may retry.", 500)






from flask import render_template


@app.before_request
def allow_html_pages():
    from flask import request
    # Allow static HTML pages to load without authentication
    if request.path in ["/", "/index.html", "/login.html", "/verify.html", "/settings.html"]:
        return None


@app.route("/")
@app.route("/index.html")
def dashboard_page():
    return render_template("index.html")

@app.route("/login.html")
def login_page():
    return render_template("login.html")

@app.route("/verify.html")
def verify_page():
    return render_template("verify.html")

@app.route("/settings.html")
def settings_page():
    return render_template("settings.html")

if __name__ == "__main__":
    # Development only. Use Gunicorn in production.
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "5000")), debug=False)
