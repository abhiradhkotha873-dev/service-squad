import asyncio
import json
import logging

from . import quota
from .cache import make_cache_key, get_cached, set_cached
from .config import get_settings
from .db import close_pool, get_pool
from .search_service import run_search

LOGGER = logging.getLogger("worker")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


async def _upsert_lead(conn, rec: dict, meta: dict) -> str:
    link = rec["link"]
    domain = link.split("/")[2] if "//" in link and "/" in link else None
    row = await conn.fetchrow(
        """
        INSERT INTO public.opportunity_leads
            (event_hash, canonical_url, title, source_domain,
             source_kind, description, last_seen_at)
        VALUES ($1, $2, $3, $4, $5, $6, now())
        ON CONFLICT (event_hash) DO UPDATE
            SET title = EXCLUDED.title,
                source_domain = EXCLUDED.source_domain,
                source_kind = EXCLUDED.source_kind,
                description = EXCLUDED.description,
                last_seen_at = now(),
                updated_at = now()
        RETURNING id
        """,
        rec["event_hash"], link, rec["title"], domain,
        meta.get("kind"), meta.get("snippet") or "",
    )
    return str(row["id"])


async def _insert_evidence(conn, lead_id, run_id, rec, meta):
    for ctype, payload in (
        ("eligibility", meta.get("eligibility") or {}),
        ("location", meta.get("location_evidence") or {}),
    ):
        await conn.execute(
            """
            INSERT INTO public.lead_evidence
                (lead_id, run_id, source_url, excerpt,
                 classification_type, classification_method,
                 confidence, reasons)
            VALUES ($1,$2,$3,$4,$5,
                    'heuristic_keyword_regex',$6,$7::jsonb)
            """,
            lead_id, run_id, rec["link"], meta.get("snippet") or "",
            ctype, payload.get("level"),
            json.dumps(payload.get("reasons", [])),
        )


async def process_run(pool, run_row) -> None:
    run_id = run_row["id"]
    city = run_row["city"] or ""
    zip_code = run_row["zip_code"] or ""
    settings = get_settings()

    key = make_cache_key(city, zip_code, settings.search_request_budget)
    cached = await get_cached(pool, key)

    try:
        if cached is not None:
            report = cached
            cache_hit = True
        else:
            report = await run_search(city, zip_code)
            cache_hit = False
            await set_cached(
                pool, key, report, settings.search_cache_ttl_seconds
            )
    except Exception as exc:
        LOGGER.exception("Internal error processing run %s", run_id)
        await pool.execute(
            """
            UPDATE public.search_runs
            SET status='failed', completed_at=now(),
                errors = errors || $2::jsonb
            WHERE id = $1
            """,
            run_id, json.dumps([f"Internal error: {type(exc).__name__}"]),
        )
        await quota.refund_reservation(pool, run_row["reservation_id"])
        return

    status = report.get("status", "failed")
    requests = report.get("requests") or {}
    accepted = report.get("results") or []
    review = report.get("review_candidates") or []

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                UPDATE public.search_runs
                SET status=$2, completed_at=now(),
                    requests_attempted=$3,
                    requests_completed=$4,
                    requests_failed=$5,
                    warnings = warnings || $6::jsonb,
                    errors   = errors   || $7::jsonb,
                    cache_hit=$8
                WHERE id = $1
                """,
                run_id, status,
                int(requests.get("attempted", 0)),
                int(requests.get("completed", 0)),
                int(requests.get("failed", 0)),
                json.dumps(report.get("warnings", [])),
                json.dumps(report.get("errors", [])),
                cache_hit,
            )

            for rank, meta in enumerate(accepted):
                public = meta["public"]
                lead_id = await _upsert_lead(conn, public, meta)
                await conn.execute(
                    """
                    INSERT INTO public.search_run_results
                        (run_id, lead_id, requested_city, requested_zip,
                         snippet, rank, score, kind, eligibility_level,
                         location_level, volunteer_role, is_review_candidate)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,false)
                    """,
                    run_id, lead_id, city, zip_code,
                    meta.get("snippet") or "",
                    rank, meta.get("score"), meta.get("kind"),
                    (meta.get("eligibility") or {}).get("level"),
                    (meta.get("location_evidence") or {}).get("level"),
                    meta.get("volunteer_role"),
                )
                await _insert_evidence(conn, lead_id, run_id, public, meta)

            for rank, meta in enumerate(review):
                public = {
                    "title": meta["title"],
                    "link": meta["link"],
                    "snippet": meta.get("snippet") or "",
                    "event_hash": meta["event_hash"],
                    "date": "Flexible / See Link",
                    "location": meta.get("snippet") or "",
                    "city": city,
                }
                lead_id = await _upsert_lead(conn, public, meta)
                await conn.execute(
                    """
                    INSERT INTO public.search_run_results
                        (run_id, lead_id, requested_city, requested_zip,
                         snippet, rank, score, kind, eligibility_level,
                         location_level, volunteer_role, is_review_candidate)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,true)
                    """,
                    run_id, lead_id, city, zip_code,
                    meta.get("snippet") or "",
                    rank, meta.get("score"), meta.get("kind"),
                    (meta.get("eligibility") or {}).get("level"),
                    (meta.get("location_evidence") or {}).get("level"),
                    meta.get("volunteer_role"),
                )
                await _insert_evidence(conn, lead_id, run_id, public, meta)

    if status in {"complete", "partial"}:
        await quota.commit_reservation(pool, run_row["reservation_id"])
    else:
        await quota.refund_reservation(pool, run_row["reservation_id"])


async def worker_loop() -> None:
    pool = await get_pool()
    LOGGER.info("Worker started.")
    stale_tick = 0
    try:
        while True:
            try:
                stale_tick += 1
                if stale_tick >= 10:
                    stale_tick = 0
                    recovered = await quota.recover_stale_reservations(pool)
                    if recovered:
                        LOGGER.info("Recovered %s stale reservations.", recovered)

                async with pool.acquire() as conn:
                    row = await conn.fetchrow(
                        """
                        UPDATE public.search_runs
                        SET status='running', started_at=now()
                        WHERE id = (
                            SELECT id FROM public.search_runs
                            WHERE status='queued'
                            ORDER BY created_at
                            FOR UPDATE SKIP LOCKED
                            LIMIT 1
                        )
                        RETURNING id, user_id, city, zip_code, reservation_id
                        """
                    )
                if row is not None:
                    await process_run(pool, row)
                else:
                    await asyncio.sleep(3)
            except Exception:
                LOGGER.exception("Worker loop error; backing off.")
                await asyncio.sleep(5)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(worker_loop())
