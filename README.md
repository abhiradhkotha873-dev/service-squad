# Volunteer SaaS Phase 3 backend

## Files
- `app.py`: Flask API and Stripe webhook.
- `calendar_utils.py`: Google Calendar link generator.
- `digest.py`: Resend daily digest sender.
- `.env.example`: environment-variable template (copy to `.env`).
- `requirements.txt`: Python dependencies.

The Phase 1 `scraper.py` must be placed beside `app.py`. Its public contract is
`scrape_events(city, zip_code)` and it must return six-key event dictionaries.

## Install and run
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Fill in .env values before starting.
gunicorn --workers 3 --threads 4 --timeout 90 app:app
```

`app.py` intentionally fails fast if any required environment variable is missing.
Use the exact origin (scheme + host + optional port, no path) for `APP_BASE_URL`.
For local frontend development on a different port, configure that exact origin as
`APP_BASE_URL`; production CORS intentionally allows only one origin.

## Daily digest
Schedule `python digest.py` once per day with cron or a scheduler. The in-process
idempotency lock prevents duplicate calls within a single process for the same UTC
date; it is not a durable distributed lock. If running multiple workers/hosts or
requiring crash-safe exactly-once delivery, add a durable job/lock mechanism without
changing the required database schema.

## Important integration notes
- Supabase Auth email confirmation must be enabled. Frontend uses Supabase JS for
  sign-in/sign-up and sends the current `session.access_token` as a Bearer token.
- Configure Stripe webhook endpoint `/api/webhook/stripe` for:
  `checkout.session.completed`, `customer.subscription.updated`,
  `customer.subscription.deleted`.
- Set `STRIPE_PRICE_ID` to the recurring $4.99/month price.
- The specified schema has no PDF export endpoint and no user-event listing/status
  endpoint; this backend does not invent extra tables or endpoints.
- The schema does not have a durable digest-sent field. Idempotency therefore lasts
  only within the current Python process, as documented in `digest.py`.
- In the Phase 1 scraper, event dates may be `"Flexible / See Link"`. The calendar
  utility intentionally uses today's UTC date for unparseable dates.
