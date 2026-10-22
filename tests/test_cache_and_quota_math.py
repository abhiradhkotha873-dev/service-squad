from datetime import date

from app.cache import make_cache_key
from app.quota import utc_month_start


def test_cache_key_normalization():
    a = make_cache_key("Chicago, IL", "60614", 3)
    b = make_cache_key("  chicago, il ", "60614", 3)
    assert a == b
    c = make_cache_key("Chicago, IL", "60614", 4)
    assert a != c


def test_utc_month_start():
    assert utc_month_start(date(2025, 3, 17)) == date(2025, 3, 1)
    assert utc_month_start(date(2025, 12, 31)) == date(2025, 12, 1)
