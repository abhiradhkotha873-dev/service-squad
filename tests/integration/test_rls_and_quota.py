import asyncio
import os
import uuid

import asyncpg
import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL not set; skipping integration tests.",
)


@pytest.mark.asyncio
async def test_concurrent_quota_is_transactional():
    dsn = os.environ["TEST_DATABASE_URL"]
    pool = await asyncpg.create_pool(dsn)
    user_id = uuid.uuid4()
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO auth.users (id, email) VALUES ($1, $2)",
                user_id, f"{user_id}@example.test",
            )
            await conn.execute(
                "UPDATE public.subscriptions SET tier_id='free' WHERE user_id=$1",
                user_id,
            )

        async def try_reserve():
            from app import quota
            async with pool.acquire() as conn:
                async with conn.transaction():
                    try:
                        await quota.reserve_search_in_tx(conn, user_id)
                        return "ok"
                    except Exception as exc:
                        return type(exc).__name__

        results = await asyncio.gather(*[try_reserve() for _ in range(20)])
        assert results.count("ok") == 5
    finally:
        await pool.close()
