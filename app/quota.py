from datetime import date
import asyncpg
from fastapi import HTTPException


def utc_month_start(d: date) -> date:
    return date(d.year, d.month, 1)


async def get_tier_for_user(conn, user_id: str) -> dict:
    row = await conn.fetchrow(
        """
        SELECT t.id AS tier_id, t.display_name, t.monthly_search_quota,
               t.saved_leads_quota, s.status
        FROM public.subscriptions s
        JOIN public.subscription_tiers t ON t.id = s.tier_id
        WHERE s.user_id = $1
        """,
        user_id,
    )
    if row is not None:
        return dict(row)
    row = await conn.fetchrow(
        """
        SELECT id AS tier_id, display_name, monthly_search_quota,
               saved_leads_quota, 'active'::text AS status
        FROM public.subscription_tiers WHERE id = 'free'
        """
    )
    return dict(row)


async def reserve_search_in_tx(conn, user_id: str) -> dict:
    tier = await get_tier_for_user(conn, user_id)
    period = utc_month_start(date.today())
    await conn.execute(
        """
        INSERT INTO public.usage_counters (user_id, period_start)
        VALUES ($1, $2) ON CONFLICT (user_id, period_start) DO NOTHING
        """,
        user_id, period,
    )
    usage = await conn.fetchrow(
        """
        SELECT searches_committed, searches_reserved
        FROM public.usage_counters
        WHERE user_id = $1 AND period_start = $2
        FOR UPDATE
        """,
        user_id, period,
    )
    if (
        usage["searches_committed"] + usage["searches_reserved"]
        >= tier["monthly_search_quota"]
    ):
        raise HTTPException(
            status_code=429,
            detail=(
                f"Monthly search quota exhausted "
                f"({tier['monthly_search_quota']} on {tier['tier_id']})."
            ),
        )
    await conn.execute(
        """
        UPDATE public.usage_counters
        SET searches_reserved = searches_reserved + 1
        WHERE user_id = $1 AND period_start = $2
        """,
        user_id, period,
    )
    reservation = await conn.fetchrow(
        """
        INSERT INTO public.quota_reservations (user_id, period_start)
        VALUES ($1, $2) RETURNING id
        """,
        user_id, period,
    )
    return {
        "reservation_id": reservation["id"],
        "period_start": period,
        "tier": tier,
    }


async def commit_reservation(pool: asyncpg.Pool, reservation_id) -> None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            res = await conn.fetchrow(
                """
                SELECT user_id, period_start, status
                FROM public.quota_reservations
                WHERE id = $1 FOR UPDATE
                """,
                reservation_id,
            )
            if res is None or res["status"] != "pending":
                return
            await conn.execute(
                "UPDATE public.quota_reservations SET status = 'committed' "
                "WHERE id = $1",
                reservation_id,
            )
            await conn.execute(
                """
                UPDATE public.usage_counters
                SET searches_reserved = GREATEST(searches_reserved - 1, 0),
                    searches_committed = searches_committed + 1
                WHERE user_id = $1 AND period_start = $2
                """,
                res["user_id"], res["period_start"],
            )


async def refund_reservation(pool: asyncpg.Pool, reservation_id) -> None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            res = await conn.fetchrow(
                """
                SELECT user_id, period_start, status
                FROM public.quota_reservations
                WHERE id = $1 FOR UPDATE
                """,
                reservation_id,
            )
            if res is None or res["status"] != "pending":
                return
            await conn.execute(
                "UPDATE public.quota_reservations SET status = 'refunded' "
                "WHERE id = $1",
                reservation_id,
            )
            await conn.execute(
                """
                UPDATE public.usage_counters
                SET searches_reserved = GREATEST(searches_reserved - 1, 0)
                WHERE user_id = $1 AND period_start = $2
                """,
                res["user_id"], res["period_start"],
            )


async def recover_stale_reservations(pool: asyncpg.Pool) -> int:
    async with pool.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(
                """
                SELECT id, user_id, period_start
                FROM public.quota_reservations
                WHERE status = 'pending' AND expires_at < now()
                FOR UPDATE SKIP LOCKED
                """
            )
            for row in rows:
                await conn.execute(
                    "UPDATE public.quota_reservations SET status = 'stale' "
                    "WHERE id = $1",
                    row["id"],
                )
                await conn.execute(
                    """
                    UPDATE public.usage_counters
                    SET searches_reserved = GREATEST(searches_reserved - 1, 0)
                    WHERE user_id = $1 AND period_start = $2
                    """,
                    row["user_id"], row["period_start"],
                )
            return len(rows)


async def check_saved_quota(conn, user_id: str) -> dict:
    tier = await get_tier_for_user(conn, user_id)
    count = await conn.fetchval(
        "SELECT COUNT(*) FROM public.saved_opportunities WHERE user_id = $1",
        user_id,
    )
    if count >= tier["saved_leads_quota"]:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Saved-lead quota exhausted "
                f"({tier['saved_leads_quota']} on {tier['tier_id']})."
            ),
        )
    return {"tier": tier, "saved_count": count}
