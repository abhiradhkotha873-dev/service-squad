from datetime import date, datetime
from typing import Optional

import asyncpg
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from . import quota
from .auth import get_current_user
from .calendar_utils import google_calendar_url
from .config import get_settings
from .db import close_pool, get_pool

app = FastAPI(title="Volunteer Opportunity API")


@app.on_event("startup")
async def _startup():
    await get_pool()


@app.on_event("shutdown")
async def _shutdown():
    await close_pool()


@app.get("/health")
async def health():
    return {"ok": True}


class LocationBody(BaseModel):
    city: Optional[str] = None
    region: Optional[str] = None
    country_code: Optional[str] = Field(None, pattern=r"^[A-Z]{2}$")
    postal_code: Optional[str] = None
    latitude: Optional[float] = Field(None, ge=-90, le=90)
    longitude: Optional[float] = Field(None, ge=-180, le=180)
    timezone: Optional[str] = None


class SearchBody(BaseModel):
    city: str = ""
    zip_code: str = ""


class SaveBody(BaseModel):
    lead_id: str
    notes: Optional[str] = None


class CalendarBody(BaseModel):
    title: str
    details: str = ""
    location: Optional[str] = None
    start: Optional[str] = None
    end: Optional[str] = None
    all_day_start: Optional[str] = None
    all_day_end: Optional[str] = None


class DevUpgradeBody(BaseModel):
    tier: str


@app.get("/api/me")
async def get_me(claims=Depends(get_current_user)):
    pool = await get_pool()
    user_id = claims["sub"]
    async with pool.acquire() as conn:
        profile = await conn.fetchrow(
            """
            SELECT city, region, country_code, postal_code,
                   latitude, longitude, timezone
            FROM public.profiles WHERE id = $1
            """,
            user_id,
        )
        tier = await quota.get_tier_for_user(conn, user_id)
        usage = await conn.fetchrow(
            """
            SELECT searches_committed, searches_reserved
            FROM public.usage_counters
            WHERE user_id = $1 AND period_start = date_trunc('month', now())::date
            """,
            user_id,
        )
        saved = await conn.fetchval(
            "SELECT COUNT(*) FROM public.saved_opportunities WHERE user_id = $1",
            user_id,
        )
    return {
        "user_id": user_id,
        "profile": dict(profile) if profile else None,
        "tier": tier,
        "usage": dict(usage) if usage else {
            "searches_committed": 0, "searches_reserved": 0
        },
        "saved_count": saved,
    }


@app.put("/api/me/location")
async def update_location(body: LocationBody, claims=Depends(get_current_user)):
    pool = await get_pool()
    user_id = claims["sub"]
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO public.profiles
                (id, city, region, country_code, postal_code,
                 latitude, longitude, timezone)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
            ON CONFLICT (id) DO UPDATE
                SET city=EXCLUDED.city, region=EXCLUDED.region,
                    country_code=EXCLUDED.country_code,
                    postal_code=EXCLUDED.postal_code,
                    latitude=EXCLUDED.latitude,
                    longitude=EXCLUDED.longitude,
                    timezone=EXCLUDED.timezone,
                    updated_at=now()
            """,
            user_id, body.city, body.region, body.country_code,
            body.postal_code, body.latitude, body.longitude, body.timezone,
        )
    return {"ok": True}


@app.post("/api/searches")
async def create_search(body: SearchBody, claims=Depends(get_current_user)):
    if not body.city and not body.zip_code:
        raise HTTPException(400, "Provide city, zip_code, or both.")
    pool = await get_pool()
    user_id = claims["sub"]
    async with pool.acquire() as conn:
        async with conn.transaction():
            reservation = await quota.reserve_search_in_tx(conn, user_id)
            run = await conn.fetchrow(
                """
                INSERT INTO public.search_runs
                    (user_id, city, zip_code, status, reservation_id)
                VALUES ($1, $2, $3, 'queued', $4)
                RETURNING id
                """,
                user_id, body.city, body.zip_code, reservation["reservation_id"],
            )
            await conn.execute(
                "UPDATE public.quota_reservations SET run_id = $1 WHERE id = $2",
                run["id"], reservation["reservation_id"],
            )
    return {"run_id": str(run["id"]), "status": "queued"}


@app.get("/api/searches")
async def list_searches(claims=Depends(get_current_user)):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, status, city, zip_code, created_at, completed_at
            FROM public.search_runs
            WHERE user_id = $1
            ORDER BY created_at DESC
            LIMIT 50
            """,
            claims["sub"],
        )
    return {"searches": [dict(r) for r in rows]}


@app.get("/api/searches/{run_id}")
async def get_search(run_id: str, claims=Depends(get_current_user)):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, status, city, zip_code, started_at, completed_at,
                   requests_attempted, requests_completed, requests_failed,
                   warnings, errors, cache_hit
            FROM public.search_runs
            WHERE id = $1 AND user_id = $2
            """,
            run_id, claims["sub"],
        )
    if row is None:
        raise HTTPException(404, "Not found.")
    return dict(row)


@app.get("/api/searches/{run_id}/results")
async def get_search_results(run_id: str, claims=Depends(get_current_user)):
    pool = await get_pool()
    async with pool.acquire() as conn:
        run = await conn.fetchrow(
            "SELECT id, status FROM public.search_runs "
            "WHERE id = $1 AND user_id = $2",
            run_id, claims["sub"],
        )
        if run is None:
            raise HTTPException(404, "Not found.")
        rows = await conn.fetch(
            """
            SELECT r.rank, r.is_review_candidate, r.score, r.kind,
                   r.eligibility_level, r.location_level,
                   r.volunteer_role, r.snippet,
                   l.id AS lead_id, l.event_hash,
                   l.canonical_url AS link, l.title
            FROM public.search_run_results r
            JOIN public.opportunity_leads l ON l.id = r.lead_id
            WHERE r.run_id = $1
            ORDER BY r.is_review_candidate, r.rank
            """,
            run_id,
        )
    return {
        "run_id": run_id,
        "status": run["status"],
        "results": [dict(r) for r in rows if not r["is_review_candidate"]],
        "review_candidates": [
            dict(r) for r in rows if r["is_review_candidate"]
        ],
    }


@app.post("/api/saved")
async def save_lead(body: SaveBody, claims=Depends(get_current_user)):
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await quota.check_saved_quota(conn, claims["sub"])
            try:
                await conn.execute(
                    "INSERT INTO public.saved_opportunities "
                    "(user_id, lead_id, notes) VALUES ($1, $2, $3)",
                    claims["sub"], body.lead_id, body.notes,
                )
            except asyncpg.UniqueViolationError:
                raise HTTPException(409, "Already saved.")
            except asyncpg.ForeignKeyViolationError:
                raise HTTPException(404, "Lead not found.")
    return {"ok": True}


@app.get("/api/saved")
async def list_saved(claims=Depends(get_current_user)):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT s.id, s.notes, s.created_at,
                   l.id AS lead_id, l.title,
                   l.canonical_url AS link, l.event_hash
            FROM public.saved_opportunities s
            JOIN public.opportunity_leads l ON l.id = s.lead_id
            WHERE s.user_id = $1
            ORDER BY s.created_at DESC
            """,
            claims["sub"],
        )
    return {"saved": [dict(r) for r in rows]}


@app.delete("/api/saved/{saved_id}")
async def delete_saved(saved_id: str, claims=Depends(get_current_user)):
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM public.saved_opportunities "
            "WHERE id = $1 AND user_id = $2",
            saved_id, claims["sub"],
        )
    if result.endswith(" 0"):
        raise HTTPException(404, "Not found.")
    return {"ok": True}


@app.post("/api/calendar/link")
async def calendar_link(body: CalendarBody):
    kwargs = {
        "title": body.title,
        "details": body.details,
        "location": body.location,
    }
    try:
        if body.start or body.end:
            if not body.start or not body.end:
                raise ValueError("Both start and end are required.")
            kwargs["start"] = datetime.fromisoformat(body.start)
            kwargs["end"] = datetime.fromisoformat(body.end)
        if body.all_day_start or body.all_day_end:
            if not body.all_day_start or not body.all_day_end:
                raise ValueError(
                    "Both all_day_start and all_day_end are required."
                )
            kwargs["all_day_start"] = date.fromisoformat(body.all_day_start)
            kwargs["all_day_end"] = date.fromisoformat(body.all_day_end)
        url = google_calendar_url(**kwargs)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"url": url, "opens_draft": True}


@app.post("/api/dev/upgrade")
async def dev_upgrade(
    body: DevUpgradeBody, claims=Depends(get_current_user)
):
    settings = get_settings()
    if not settings.allow_dev_entitlement:
        raise HTTPException(403, "Dev entitlement endpoint is disabled.")
    if body.tier not in {"free", "pro"}:
        raise HTTPException(400, "Invalid tier.")
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE public.subscriptions "
            "SET tier_id = $2, updated_at = now() WHERE user_id = $1",
            claims["sub"], body.tier,
        )
    return {"ok": True, "tier": body.tier}
