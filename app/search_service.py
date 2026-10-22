import asyncio

from scraper import scrape_events_report
from .config import get_settings


def _run_sync(city: str, zip_code: str, budget: int) -> dict:
    return scrape_events_report(city, zip_code, request_budget=budget)


async def run_search(city: str, zip_code: str) -> dict:
    settings = get_settings()
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, _run_sync, city, zip_code, settings.search_request_budget
    )
