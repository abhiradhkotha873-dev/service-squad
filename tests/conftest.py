import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    yield


@pytest.fixture
def fake_tavily(monkeypatch):
    """Scripted responder. Pads with the last payload once exhausted."""

    def install(payloads):
        import scraper

        items = list(payloads)
        if not items:
            raise ValueError("fake_tavily requires at least one payload.")

        state = {"i": 0}

        def fake(session, query):
            idx = state["i"]
            state["i"] = idx + 1
            item = items[idx] if idx < len(items) else items[-1]
            if isinstance(item, Exception):
                raise item
            return item

        monkeypatch.setattr(scraper, "request_search", fake)
        return fake

    return install
