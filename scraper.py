#!/usr/bin/env python3
"""Discover and rank unverified student volunteer leads using Tavily.

Phase 2 additions:
  - scrape_events_report(city, zip_code, *, request_budget=None) returns a
    structured report with results, review_candidates, status, warnings,
    request counts, and classification evidence.
  - scrape_events(city, zip_code) preserves the Phase 1 six-key contract.
"""

import argparse
import hashlib
import json
import logging
import os
import re
import time
import unicodedata
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup


SEARCH_URL = "https://api.tavily.com/search"
LOGGER = logging.getLogger("volunteer_scraper")

TRACKING_KEYS = {
    "fbclid", "gclid", "dclid", "msclkid", "mc_cid",
    "mc_eid", "igshid", "yclid", "_ga", "_gl",
}

VOLUNTEER_PATTERN = re.compile(
    r"\bvolunteer\w*\b|\bcommunity service\b|\bservice hours\b",
    re.IGNORECASE,
)

YOUTH_PATTERN = re.compile(
    r"\bteens?\b|\bteenagers?\b|\byouth\b|\bhigh school\b"
    r"|\bsecondary school\b|\bstudents?\b|\bminors?\b"
    r"|\bunder 18\b|\bparental consent\b|\bminor consent\b",
    re.IGNORECASE,
)

STRONG_YOUTH_PATTERN = re.compile(
    r"\bteens?\b|\bteenagers?\b|\bhigh school\b"
    r"|\bsecondary school\b|\bunder 18\b|\bparental consent\b"
    r"|\bminor consent\b",
    re.IGNORECASE,
)

BENEFICIARY_PATTERN = re.compile(
    r"volunteers?\s+(?:help|mentor|tutor|teach|support|serve)\b"
    r"[^.]{0,80}\b(?:youth|teens?|children|students)\b"
    r"|(?:helping|mentoring|tutoring|teaching)\s+(?:at[- ]risk\s+)?"
    r"(?:youth|teens?|children)\b"
    r"|\bvolunteer\s+(?:to\s+)?(?:help|mentor|tutor)\b"
    r"[^.]{0,60}\b(?:youth|teens?|children)\b",
    re.IGNORECASE,
)

YOUTH_VOLUNTEER_PATTERN = re.compile(
    r"\b(?:teen|youth|junior|high[- ]school|student)\s+volunteers?\b"
    r"|\bvolunteers?\s+(?:for|age[sd]?)\s+(?:teens?|youth|high[- ]school)\b"
    r"|\bvolunteer(?:ing)?\s+program\s+for\s+(?:teens?|youth|high[- ]school)\b"
    r"|\b(?:ages?|aged)\s+\d{1,2}\s*(?:-|–|to)\s*\d{1,2}\b"
    r"|\bages?\s+\d{1,2}\s+(?:and\s+(?:up|older)|or\s+(?:older|above))\b",
    re.IGNORECASE,
)


class SearchError(RuntimeError):
    """The search could not be completed reliably."""


def normalize_text(value):
    return " ".join(str(value or "").split())


def plain_text(value):
    if not value:
        return ""
    soup = BeautifulSoup(str(value), "html.parser")
    return normalize_text(soup.get_text(" ", strip=True))


def comparable(value):
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = value.casefold()
    return " ".join(re.findall(r"\w+", value, flags=re.UNICODE))


def contains_phrase(text, phrase):
    text = comparable(text)
    phrase = comparable(phrase)
    return bool(phrase) and f" {phrase} " in f" {text} "


def clean_url(raw_url):
    if not isinstance(raw_url, str):
        return None
    try:
        parts = urlsplit(raw_url.strip())
        scheme = parts.scheme.lower()
        host = (parts.hostname or "").lower()
        if scheme not in {"http", "https"} or not host:
            return None
        if parts.username or parts.password:
            return None
        port = parts.port
        netloc = f"[{host}]" if ":" in host else host
        if port and not (
            (scheme == "https" and port == 443)
            or (scheme == "http" and port == 80)
        ):
            netloc += f":{port}"
        query = [
            (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not k.lower().startswith("utm_")
            and k.lower() not in TRACKING_KEYS
        ]
        return urlunsplit((scheme, netloc, parts.path or "/", urlencode(query), ""))
    except (ValueError, UnicodeError):
        return None


def make_event_hash(link):
    return hashlib.sha256(link.encode("utf-8")).hexdigest()


def build_queries(city, zip_code):
    safe_city = city.replace('"', " ")
    safe_zip = zip_code.replace('"', " ")
    primary_place = safe_city or safe_zip
    full_place = normalize_text(f"{safe_city} {safe_zip}")
    return list(dict.fromkeys([
        f"{full_place} teen volunteer opportunities",
        f"{primary_place} high school student volunteering",
        f"{primary_place} youth community service volunteer",
    ]))


def request_search(session, query):
    response = session.post(
        SEARCH_URL,
        json={
            "query": query,
            "topic": "general",
            "search_depth": "basic",
            "auto_parameters": False,
            "max_results": 8,
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
        },
        timeout=(10, 45),
    )
    if response.status_code in {401, 403}:
        raise SearchError("Tavily rejected access. Check your API key and account.")
    if response.status_code == 429:
        raise SearchError("Tavily rate-limited this request. Wait before trying again.")
    if response.status_code in {402, 432, 433}:
        raise SearchError(
            "Tavily reported a payment or usage-limit error "
            f"(HTTP {response.status_code}). Check your dashboard."
        )
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError as exc:
        raise SearchError("Tavily returned invalid JSON.") from exc
    if not isinstance(payload, dict):
        raise SearchError("Unexpected Tavily response structure.")
    if payload.get("error"):
        raise SearchError("Tavily returned an API error.")
    if not isinstance(payload.get("results"), list):
        raise SearchError("Tavily response has no valid results list.")
    return payload["results"]


def _classify_kind(title, snippet, link):
    url_l = link.lower()
    title_l = title.casefold()
    if any(x in url_l for x in (
        "/directory", "/listings", "/find-",
        "opportunity-finder", "teenlife.com/directory",
    )):
        return "directory"
    if any(h in title_l for h in (
        "volunteer directory", "opportunities directory", "find volunteer",
        "volunteer match",
    )):
        return "directory"
    if any(h in title_l for h in (
        "how to ", "ways to ", " a guide", "guide to ",
        "tips for ", "benefits of ", "why you should",
        "top 10 ", "top ten ", "best volunteer", "where to volunteer",
    )):
        return "article"
    if any(h in title_l for h in (
        "volunteer program", "volunteer opportunity", "junior volunteer",
        "teen volunteer", "youth volunteer", "volunteer corps",
        "volunteer with", "volunteer at",
    )):
        return "direct_program"
    return "unknown"


def _classify_eligibility(text):
    strong = bool(STRONG_YOUTH_PATTERN.search(text))
    youth_vol = bool(YOUTH_VOLUNTEER_PATTERN.search(text))
    if strong and youth_vol:
        return "explicit", ["strong youth/teen language", "youth volunteer signal"]
    if youth_vol:
        return "ambiguous", [
            "youth volunteer signal without strong teen-specific language"
        ]
    if strong:
        return "ambiguous", ["youth mentioned but volunteer role for youth unclear"]
    if YOUTH_PATTERN.search(text):
        return "ambiguous", ["generic 'students'/'youth' mention"]
    return "unknown", ["no youth eligibility language found"]


def _classify_location(title, snippet, link, city, zip_code):
    city_name = city.split(",", 1)[0].strip() if city else ""
    text = f"{title} {snippet}"
    matched_city = contains_phrase(text, city_name) if city_name else False
    matched_zip = False
    if zip_code:
        matched_zip = contains_phrase(text, zip_code)
        if not matched_zip:
            compact_zip = comparable(zip_code).replace(" ", "")
            tokens = re.findall(r"\b[A-Za-z0-9]+(?:[ -][A-Za-z0-9]+)?\b", text)
            matched_zip = any(
                comparable(t).replace(" ", "") == compact_zip for t in tokens
            )
    url_l = comparable(unquote(link))
    url_has_city = contains_phrase(url_l, city_name) if city_name else False
    url_has_zip = bool(
        zip_code and comparable(zip_code).replace(" ", "") in url_l.replace(" ", "")
    )
    if matched_city or matched_zip:
        reasons = []
        if matched_city:
            reasons.append("city name in title/snippet")
        if matched_zip:
            reasons.append("postal code in title/snippet")
        return "explicit", reasons
    if url_has_city or url_has_zip:
        return "ambiguous", ["location signal only in URL"]
    return "unknown", ["no location evidence in title, snippet, or URL"]


def _classify_volunteer_role(snippet):
    # Classify on the snippet only, NOT on title+snippet concatenated.
    # Concatenation produces false positives like "Help Youth" + "Volunteers
    # help teens ..." matching the youth-volunteer pattern "youth volunteers".
    if BENEFICIARY_PATTERN.search(snippet):
        return "youth_beneficiaries", [
            "youth appear to be program beneficiaries, not volunteers"
        ]
    if YOUTH_VOLUNTEER_PATTERN.search(snippet):
        return "youth_volunteers", ["explicit youth-volunteer language"]
    return "unclear", ["volunteer role for youth not clearly established"]


def assess_result(title, snippet, link, city, zip_code):
    """Return (score, rejection_reason, evidence). Score is None if rejected."""
    raw = f"{title} {snippet} {unquote(link)}"
    text = comparable(raw)

    kind = _classify_kind(title, snippet, link)
    eligibility_level, elig_reasons = _classify_eligibility(f"{title} {snippet}")
    location_level, loc_reasons = _classify_location(
        title, snippet, link, city, zip_code
    )
    volunteer_role, vr_reasons = _classify_volunteer_role(snippet)

    evidence = {
        "kind": kind,
        "eligibility": {"level": eligibility_level, "reasons": elig_reasons},
        "location_evidence": {"level": location_level, "reasons": loc_reasons},
        "volunteer_role": volunteer_role,
        "volunteer_role_reasons": vr_reasons,
        "has_volunteer": bool(VOLUNTEER_PATTERN.search(text)),
        "has_youth": bool(YOUTH_PATTERN.search(text)),
        "has_strong_youth": bool(STRONG_YOUTH_PATTERN.search(text)),
        "has_youth_volunteer": bool(YOUTH_VOLUNTEER_PATTERN.search(raw)),
        "has_location": location_level == "explicit",
    }

    if not evidence["has_volunteer"]:
        return None, "no volunteer/community-service evidence", evidence
    if not (evidence["has_strong_youth"] or evidence["has_youth_volunteer"]):
        return None, "youth mention is generic; not clearly for teen volunteers", evidence
    if volunteer_role == "youth_beneficiaries":
        return None, "youth appear to be beneficiaries, not volunteers", evidence
    if not evidence["has_location"]:
        return None, "no explicit city/postal-code evidence in snippet", evidence

    score = 4
    score += 3 if evidence["has_location"] else 0
    score += 4 if evidence["has_strong_youth"] else 1
    if VOLUNTEER_PATTERN.search(comparable(title)):
        score += 3
    if YOUTH_PATTERN.search(comparable(title)):
        score += 2
    return score, "", evidence


def scrape_events_report(city, zip_code, *, request_budget=None):
    if not isinstance(city, str) or not isinstance(zip_code, str):
        raise ValueError("city and zip_code must both be strings.")
    city = normalize_text(city)
    zip_code = normalize_text(zip_code)
    if not city and not zip_code:
        raise ValueError("Provide a city, postal code, or both.")
    if len(city) > 200 or len(zip_code) > 32:
        raise ValueError("City or postal code is too long.")

    report = {
        "status": "failed",
        "city": city,
        "zip_code": zip_code,
        "results": [],
        "review_candidates": [],
        "warnings": [],
        "errors": [],
        "requests": {"attempted": 0, "completed": 0, "failed": 0},
        "examined": 0,
        "queries": [],
    }

    api_key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not api_key:
        report["errors"].append("TAVILY_API_KEY is not set.")
        return report

    queries = build_queries(city, zip_code)
    report["queries"] = queries
    budget = request_budget if request_budget is not None else len(queries)

    accepted = {}
    review = {}
    completed = 0
    examined = 0
    failures = []

    with requests.Session() as session:
        session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": "VolunteerDiscovery/4.1",
        })

        for index, query in enumerate(queries):
            if report["requests"]["attempted"] >= budget:
                report["warnings"].append(
                    f"Request budget of {budget} reached; skipping remaining queries."
                )
                break
            if index:
                time.sleep(1.0)

            LOGGER.info("Query %s: %s", index + 1, query)
            report["requests"]["attempted"] += 1

            try:
                results = request_search(session, query)
            except SearchError as exc:
                failures.append(str(exc))
                report["errors"].append(str(exc))
                report["requests"]["failed"] += 1
                LOGGER.warning("%s", exc)
                break
            except requests.RequestException as exc:
                status = exc.response.status_code if exc.response is not None else None
                message = (
                    f"Search failed: HTTP {status}."
                    if status is not None
                    else f"Network failure: {type(exc).__name__}."
                )
                failures.append(message)
                report["errors"].append(message)
                report["requests"]["failed"] += 1
                LOGGER.warning("%s", message)
                continue

            completed += 1
            report["requests"]["completed"] += 1
            LOGGER.info("Received %s raw results.", len(results))

            for item in results:
                examined += 1
                if not isinstance(item, dict):
                    continue

                title = plain_text(item.get("title"))
                snippet = plain_text(item.get("content"))
                link = clean_url(item.get("url"))

                if not title or not link:
                    continue

                score, reason, evidence = assess_result(
                    title, snippet, link, city, zip_code
                )
                link_hash = make_event_hash(link)
                base_meta = {
                    "title": title,
                    "link": link,
                    "snippet": snippet,
                    "event_hash": link_hash,
                    "kind": evidence["kind"],
                    "eligibility": evidence["eligibility"],
                    "location_evidence": evidence["location_evidence"],
                    "volunteer_role": evidence["volunteer_role"],
                    "score": score,
                    "rejection_reasons": [reason] if reason else [],
                }

                if score is not None:
                    public = {
                        "title": title,
                        "date": "Flexible / See Link",
                        "location": snippet or "Location not confirmed; see link.",
                        "city": city,
                        "link": link,
                        "event_hash": link_hash,
                    }
                    meta = {"public": public, **base_meta}
                    candidate = (score, len(snippet))
                    previous = accepted.get(link_hash)
                    if previous is None or candidate > previous[0]:
                        accepted[link_hash] = (candidate, meta)
                    continue

                signals = (
                    int(evidence["has_volunteer"])
                    + int(evidence["has_youth"])
                    + int(evidence["has_location"])
                )
                if evidence["has_volunteer"] and signals >= 2:
                    review[link_hash] = base_meta

    report["examined"] = examined

    if completed == 0:
        report["status"] = "failed"
        msg = "No search completed successfully."
        if failures:
            msg += " " + " | ".join(failures)
        report["errors"].append(msg)
        return report

    if failures:
        report["status"] = "partial"
        report["warnings"].append("Results are partial because some searches failed.")
    else:
        report["status"] = "complete"

    if not accepted and not review:
        report["warnings"].append(
            "No results passed the relevance filters. This does not prove "
            "that no opportunities exist. If using postal code only, try "
            "supplying a city as well."
        )

    ranked = sorted(
        accepted.values(),
        key=lambda entry: (-entry[0][0], -entry[0][1], entry[1]["title"].casefold()),
    )
    report["results"] = [meta for _, meta in ranked]
    report["review_candidates"] = sorted(
        review.values(),
        key=lambda item: (-(item["score"] or 0), item["title"].casefold()),
    )
    return report


def scrape_events(city, zip_code):
    """Phase 1 public contract preserved: list of six-key dicts."""
    report = scrape_events_report(city, zip_code)
    if report["status"] == "failed":
        raise SearchError(" ".join(report["errors"]) or "All searches failed.")
    return [r["public"] for r in report["results"]]


def main():
    parser = argparse.ArgumentParser(
        description="Find and rank student volunteer leads using Tavily."
    )
    parser.add_argument("--city", default="")
    parser.add_argument("--zip-code", default="")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--report", action="store_true",
        help="Emit the full structured report instead of the six-key results.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    try:
        if args.report:
            payload = scrape_events_report(args.city, args.zip_code)
        else:
            payload = scrape_events(args.city, args.zip_code)
    except (ValueError, SearchError) as exc:
        parser.exit(1, f"Error: {exc}\n")

    print(json.dumps(payload, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
