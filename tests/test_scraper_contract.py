import hashlib

import pytest

import scraper


def _result(title, content, url):
    return {"title": title, "content": content, "url": url}


def test_public_contract_six_keys(fake_tavily):
    fake_tavily([[
        _result(
            "Teen Volunteer Program",
            "High school students ages 14-17 in Springfield can volunteer.",
            "https://example.org/vol?utm_source=x&keep=1",
        ),
    ]])
    results = scraper.scrape_events("Springfield, IL", "62701")
    assert len(results) >= 1
    r = results[0]
    assert set(r) == {
        "title", "date", "location", "city", "link", "event_hash"
    }
    assert r["date"] == "Flexible / See Link"
    assert r["city"] == "Springfield, IL"
    assert r["link"] == "https://example.org/vol?keep=1"
    assert len(r["event_hash"]) == 64
    assert hashlib.sha256(r["link"].encode()).hexdigest() == r["event_hash"]


def test_dedup_keeps_best(fake_tavily):
    fake_tavily([
        [_result("Teen Volunteers", "Teens in Boston", "https://a.org/x")],
        [_result("Teen Volunteers Boston", "Teens in Boston", "https://a.org/x?utm_source=y")],
        [_result("Different", "Teens in Boston", "https://b.org/y")],
    ])
    results = scraper.scrape_events("Boston, MA", "")
    urls = [r["link"] for r in results]
    assert len(urls) == len(set(urls))


def test_rejects_generic_students(fake_tavily):
    fake_tavily([[
        _result(
            "University Volunteers",
            "College students can volunteer with us.",
            "https://u.edu/volunteer",
        ),
    ]])
    report = scraper.scrape_events_report("Boston, MA", "")
    assert all(r["public"]["title"] != "University Volunteers" for r in report["results"])


def test_rejects_youth_beneficiary(fake_tavily):
    fake_tavily([[
        _result(
            "Help Youth",
            "Volunteers help teens with homework in Boston.",
            "https://help.org/vol",
        ),
    ]])
    report = scraper.scrape_events_report("Boston, MA", "")
    assert all(r["public"]["title"] != "Help Youth" for r in report["results"])


def test_missing_location_goes_to_review(fake_tavily):
    fake_tavily([[
        _result(
            "Teen Volunteer Program",
            "High school students ages 14-17 can volunteer.",
            "https://example.org/teen",
        ),
    ]])
    report = scraper.scrape_events_report("Chicago, IL", "60614")
    assert report["results"] == []
    assert len(report["review_candidates"]) == 1


def test_failure_raises(fake_tavily):
    fake_tavily([scraper.SearchError("boom")])
    with pytest.raises(scraper.SearchError):
        scraper.scrape_events("Chicago, IL", "")


def test_empty_not_success(fake_tavily):
    fake_tavily([[], [], []])
    report = scraper.scrape_events_report("Nowhere, ZZ", "")
    assert report["status"] == "complete"
    assert report["results"] == []
    assert report["warnings"]

def test_title_snippet_concatenation_does_not_create_youth_volunteer():
    """Regression: old code classified f"{title} {snippet}", which let a
    generic "Help Youth" title + a beneficiary-only snippet match the
    youth-volunteer pattern. Classification must use the snippet alone.
    """
    import scraper
    title = "Help Youth"
    snippet = "Volunteers help teens with homework after school."
    role, _reasons = scraper._classify_volunteer_role(snippet)
    assert role == "youth_beneficiaries"
