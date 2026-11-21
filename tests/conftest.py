"""Shared test helpers: fake HTTP responses, an in-memory store, the real config."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from src import jobs as _jobs

_REAL_ASSERT_PUBLIC = _jobs._assert_public

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeResponse:
    def __init__(self, payload, status_code: int = 200, headers: dict | None = None):
        self._payload, self.status_code, self.headers = payload, status_code, headers or {}

    def json(self):
        return self._payload


@pytest.fixture(scope="session")
def cfg():
    return yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))


@pytest.fixture()
def no_sleep(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *_: None)


class FakeStore:
    """In-memory stand-in for SupabaseStore's careers methods. Counts writes."""

    def __init__(self, companies, careers=None, jobs=None):
        self.companies = companies
        self.careers = {k: dict(v) for k, v in (careers or {}).items()}
        self.jobs: list[dict] = [dict(j) for j in (jobs or [])]
        self._next_id = max([j.get("id", 0) for j in self.jobs] + [0]) + 1
        self.writes = 0

    def deal_companies(self):
        return list(self.companies)

    def careers_all(self):
        return {k: dict(v) for k, v in self.careers.items()}

    def careers_upsert(self, row):
        self.writes += 1
        self.careers.setdefault(row["company_key"], {}).update(row)

    def jobs_all(self):
        out: dict[str, list[dict]] = {}
        for j in self.jobs:
            out.setdefault(j["company_key"], []).append(dict(j))
        return out

    def jobs_upsert(self, rows):
        self.writes += 1
        for r in rows:
            for j in self.jobs:
                if j["company_key"] == r["company_key"] and j["external_id"] == r["external_id"]:
                    j.update(r)
                    break
            else:
                self.jobs.append({"id": self._next_id, **r})
                self._next_id += 1

    def jobs_close(self, ids, today):
        self.writes += 1
        for j in self.jobs:
            if j["id"] in ids:
                j["is_open"], j["closed_at"] = False, today


@pytest.fixture(autouse=True)
def fresh_limiters():
    """Every test starts with new, instant (no spacing) host limiters."""
    from src import jobs
    jobs.configure({"request_delay": 0, "host_min_interval": {}, "rate_limit_trip_after": 6, "rate_limit_cooldown": 300})
    yield
    jobs.configure({"request_delay": 0, "host_min_interval": {}})


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    """A unit test that forgets to fake its HTTP calls fails loudly instead of hitting the internet."""
    def refuse(*args, **kwargs):
        raise AssertionError(f"unexpected real HTTP request in a unit test: {args[:1]}")
    monkeypatch.setattr(_jobs.requests, "get", refuse)
    monkeypatch.setattr(_jobs, "_assert_public", lambda host: None)     # no DNS lookups either


@pytest.fixture()
def real_assert_public():
    return _REAL_ASSERT_PUBLIC
