from typing import Optional
import hashlib
import json
from datetime import datetime, timedelta, timezone
import asyncpg


def make_cache_key(city: str, zip_code: str, budget: int) -> str:
    raw = json.dumps(
        {"c": (city or "").strip().casefold(),
         "z": (zip_code or "").strip().casefold(),
         "b": int(budget)},
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def get_cached(pool: asyncpg.Pool, key: str) -> Optional[dict]:
    row = await pool.fetchrow(
        "SELECT payload FROM public.search_cache "
        "WHERE cache_key = $1 AND expires_at > now()",
        key,
    )
    if row is None:
        return None
    payload = row["payload"]
    if isinstance(payload, str):
        try:
            return json.loads(payload)
        except ValueError:
            return None
    return payload


async def set_cached(
    pool: asyncpg.Pool, key: str, payload: dict, ttl_seconds: int
) -> None:
    if payload.get("status") not in {"complete", "partial"}:
        return
    if payload.get("requests", {}).get("completed", 0) == 0:
        return
    expires = datetime.now(timezone.utc) + timedelta(seconds=int(ttl_seconds))
    await pool.execute(
        """
        INSERT INTO public.search_cache (cache_key, payload, expires_at)
        VALUES ($1, $2::jsonb, $3)
        ON CONFLICT (cache_key) DO UPDATE
            SET payload = EXCLUDED.payload,
                expires_at = EXCLUDED.expires_at,
                created_at = now()
        """,
        key, json.dumps(payload, default=str), expires,
    )


async def purge_expired(pool: asyncpg.Pool) -> None:
    await pool.execute(
        "DELETE FROM public.search_cache WHERE expires_at < now()"
    )
