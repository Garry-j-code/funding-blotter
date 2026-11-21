"""Offline tests for src/jobs.py: no network, no database."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from src import jobs
from src.jobs import Board, Posting, RoleMatcher
from tests.conftest import FakeResponse, FakeStore, fixture

TODAY = date(2026, 11, 21)


# ----------------------------------------------------------------------
# Title matching
# ----------------------------------------------------------------------
@pytest.fixture(scope="module")
def matcher(cfg):
    return RoleMatcher(cfg["jobs"])


@pytest.mark.parametrize("title, primary", [
    ("Forward Deployed Engineer", "forward_deployed_engineer"),
    ("AI Engineer - FDE (Forward Deployed Engineer)", "forward_deployed_engineer"),
    ("Lead, Forward Deployed Engineering", "forward_deployed_engineer"),
    ("Senior FDE", "forward_deployed_engineer"),
    ("Product Engineer", "product_engineer"),
    ("Senior Product Engineer", "product_engineer"),
    ("Founding Product Engineer", "product_engineer"),
    ("AI Product Engineer", "ai_product_engineer"),
    ("Product Engineer, AI", "ai_product_engineer"),
    ("Product Engineer (AI)", "ai_product_engineer"),
    ("AI Engineer", "ai_engineer"),
    ("Senior AI Engineer", "ai_engineer"),
    ("Applied AI Engineer", "ai_engineer"),
    ("Senior Applied AI Engineer", "ai_engineer"),
    ("AI/ML Engineer", "ai_engineer"),
    ("LLM Engineer", "ai_engineer"),
    ("GenAI Engineer", "ai_engineer"),
    ("AI Software Engineer", "ai_engineer"),
    ("Founding Staff AI Engineer", "ai_engineer"),
    ("AI Engineer (Korea)", "ai_engineer"),
    ("Forward Deployed Analytics Engineer", "forward_deployed_engineer"),
    ("Forward Deployed Engineer, Banking", "forward_deployed_engineer"),
    ("Full Stack / Product Engineer", "product_engineer"),
    ("Member of Technical Staff, Product Engineer", "product_engineer"),
])
def test_titles_that_match(matcher, title, primary):
    got = matcher.match(title)
    assert got is not None, title
    assert got[0] == primary
    assert primary in got[1]


@pytest.mark.parametrize("title", [
    "Account Executive", "Founding Recruiter", "Product Designer", "Product Manager",
    "Engineering Manager", "Director of AI Engineering", "AI Sales Engineer",
    "Head of AI", "Marketing Manager, AI", "Fund Accountant", "Software Engineer",
    "Senior Backend Engineer", "Data Analyst", "Maintenance Engineer", "VP of Engineering",
    "Operations Associate",
    # strict rule: close, but not one of the four roles
    "Forward Deployed Banker, EMEA", "Forward Deployed Auditor", "Forward Deployed, Equity Research",
    "Forward Deployed Researcher - Data as a Service", "Machine Learning Engineer", "ML Engineer",
    "Sr. Machine Learning Engineer, Ads", "AI Security Engineer", "AI Operations Engineer",
    "AI Automation Engineer", "AI Enablement Engineer", "Senior AI Infrastructure Engineer",
    "Software Engineer (AI-Native)", "Senior / Staff Software Engineer, AI", "Backend Engineer, AI",
    "Software Engineer - Applied AI", "Software Engineer, Backend | Product Engineering - SF Only",
])
def test_titles_that_do_not_match(matcher, title):
    assert matcher.match(title) is None, title


def test_all_matching_roles_are_kept_in_tags_and_primary_is_the_first(matcher):
    primary, tags = matcher.match("AI Engineer - FDE (Forward Deployed Engineer)")
    assert primary == "forward_deployed_engineer"
    assert tags == ["forward_deployed_engineer", "ai_engineer"]


# ----------------------------------------------------------------------
# Names, URLs, verification
# ----------------------------------------------------------------------
def test_clean_name_drops_fka_and_suffixes():
    assert jobs.clean_name("Proxima (formerly VantAI)") == "Proxima"
    assert jobs.clean_name("Valitana") == "Valitana"
    assert jobs.clean_name("RIIG Technology, Inc , doing business as HOOTL") == "RIIG Technology, Inc"


def test_slug_candidates_cover_joined_hyphenated_and_suffix_free_forms():
    got = jobs.slug_candidates("Hanover Park")
    assert got[:2] == ["hanoverpark", "hanover-park"]
    assert "plurio" in jobs.slug_candidates("Plurio Labs")
    assert "kindo" in jobs.slug_candidates("Kindo AI")
    assert len(jobs.slug_candidates("A Very Long Company Name Holdings Group Inc", limit=3)) <= 3


@pytest.mark.parametrize("url, expected", [
    ("https://jobs.ashbyhq.com/hanover-park", ("ashby", "hanover-park")),
    ("https://jobs.ashbyhq.com/hanover-park/0b390016-7eb8", ("ashby", "hanover-park")),
    ("https://boards.greenhouse.io/databricks", ("greenhouse", "databricks")),
    ("https://job-boards.greenhouse.io/databricks/jobs/123", ("greenhouse", "databricks")),
    ("https://boards.greenhouse.io/embed/job_board?for=acme", ("greenhouse", "acme")),
    ("https://jobs.lever.co/leverdemo", ("lever", "leverdemo")),
    ("https://apply.workable.com/huggingface/", ("workable", "huggingface")),
    ("https://huggingface.workable.com/", ("workable", "huggingface")),
    ("https://www.acme.com/careers", None),
    ("https://example.com/jobs.ashbyhq.com/x", None),
    ("not a url", None),
])
def test_parse_board_url(url, expected):
    assert jobs.parse_board_url(url) == expected


@pytest.mark.parametrize("bad", [
    "", "   ", "javascript:alert(1)", "ftp://example.com/jobs", "https://", "https://localhost/x",
    "https://user:pass@example.com/jobs", "https://example.com/has space", "https://example.com/" + "a" * 600,
])
def test_validate_url_rejects(bad):
    with pytest.raises(ValueError):
        jobs.validate_url(bad)


def test_validate_url_accepts_http_and_https():
    assert jobs.validate_url(" https://jobs.ashbyhq.com/acme ") == "https://jobs.ashbyhq.com/acme"
    assert jobs.validate_url("http://acme.com/careers") == "http://acme.com/careers"


def _board(name=None, text="", ats="ashby", slug="x"):
    return Board(ats, slug, name, [], text)


def test_verify_exact_and_containing_board_names():
    assert jobs.verify_board("Hanover Park", _board("Hanover Park"))
    assert jobs.verify_board("Databricks", _board("Databricks, Inc."))
    assert not jobs.verify_board("Rain", _board("Rainforest Cafe Group"))   # short name: exact only


def test_verify_by_posting_text_for_long_names():
    assert jobs.verify_board("Hanover Park", _board(text="WHAT HANOVER PARK DOES builds AI systems"))
    assert not jobs.verify_board("Hanover Park", _board(text="a different company entirely"))


def test_verify_short_names_need_an_exact_board_name():
    assert not jobs.verify_board("De", _board(text="we build de-identification tools"))
    assert not jobs.verify_board("Aqua", _board("Aqua Security"))
    assert jobs.verify_board("Aqua", _board("Aqua"))


# ----------------------------------------------------------------------
# Adapters (HTTP is faked with the saved fixtures)
# ----------------------------------------------------------------------
def test_ashby_adapter(monkeypatch):
    monkeypatch.setattr(jobs, "_get", lambda url, params=None, attempts=3: FakeResponse(fixture("ashby_hanover_park.json")))
    board = jobs.fetch_ashby("hanover-park")
    assert board.ats == "ashby" and len(board.postings) == 3
    pe = next(p for p in board.postings if p.title == "Product Engineer")
    assert pe.url.startswith("https://jobs.ashbyhq.com/hanover-park/") and pe.external_id
    assert pe.location == "New York City" and pe.posted_at
    assert "hanover park" in board.text_sample.lower()


def test_greenhouse_adapter(monkeypatch):
    data = fixture("greenhouse_databricks.json")

    def fake_get(url, params=None, attempts=3):
        return FakeResponse({"name": data["meta"]["name"]} if url.endswith("/databricks") else {"jobs": data["jobs"]})

    monkeypatch.setattr(jobs, "_get", fake_get)
    board = jobs.fetch_greenhouse("databricks")
    assert board.company_name == "Databricks" and len(board.postings) == 3
    assert all(p.url.startswith("http") and p.external_id for p in board.postings)


def test_lever_adapter(monkeypatch):
    monkeypatch.setattr(jobs, "_get", lambda url, params=None, attempts=3: FakeResponse(fixture("lever_leverdemo.json")))
    board = jobs.fetch_lever("leverdemo")
    assert board.ats == "lever" and board.postings
    assert all(p.title and p.url.startswith("http") and p.external_id for p in board.postings)


def test_workable_adapter(monkeypatch):
    monkeypatch.setattr(jobs, "_get", lambda url, params=None, attempts=3: FakeResponse(fixture("workable_sample.json")))
    board = jobs.fetch_workable("huggingface")
    assert board.ats == "workable" and board.company_name and board.postings
    assert all(p.external_id and p.url.startswith("http") for p in board.postings)


@pytest.mark.parametrize("fetcher", [jobs.fetch_ashby, jobs.fetch_greenhouse, jobs.fetch_lever, jobs.fetch_workable])
def test_adapters_return_none_for_a_missing_board(monkeypatch, fetcher):
    monkeypatch.setattr(jobs, "_get", lambda url, params=None, attempts=3: None)
    assert fetcher("nope") is None


def test_get_retries_then_raises_on_server_errors(monkeypatch, no_sleep):
    calls = []

    def fake(url, headers=None, params=None, timeout=None):
        calls.append(url)
        return FakeResponse({}, 503)

    monkeypatch.setattr(jobs.requests, "get", fake)
    with pytest.raises(jobs.FetchError):
        jobs._get("https://example.com/x")
    assert len(calls) == 3


def test_get_does_not_retry_client_errors_and_maps_404_to_none(monkeypatch, no_sleep):
    seq = iter([FakeResponse({}, 404)])
    monkeypatch.setattr(jobs.requests, "get", lambda *a, **k: next(seq))
    assert jobs._get("https://example.com/x") is None
    seq2 = iter([FakeResponse({}, 403)])
    monkeypatch.setattr(jobs.requests, "get", lambda *a, **k: next(seq2))
    with pytest.raises(jobs.FetchError):
        jobs._get("https://example.com/x")


def test_matching_jobs_filters_and_normalises(matcher):
    board = Board("ashby", "x", None, [
        Posting("1", "Product Engineer", "Eng", "NYC", "FullTime", "https://j/1", "2026-11-19T16:45:30.237+00:00"),
        Posting("2", "Account Executive", "GTM", "NYC", "FullTime", "https://j/2", None),
        Posting("3", "AI Engineer", "Eng", "NYC", "FullTime", "https://j/3", "not-a-date"),
        Posting("", "AI Engineer", "", "", "", "https://j/4", None),     # no id: skipped
    ])
    out = jobs.matching_jobs(board, matcher)
    assert [m["external_id"] for m in out] == ["1", "3"]
    assert out[0]["posted_at"].startswith("2026-11-19") and out[1]["posted_at"] is None


# ----------------------------------------------------------------------
# plan_sync: the rules that protect your data
# ----------------------------------------------------------------------
def _m(ext, title="Product Engineer"):
    return {"external_id": ext, "title": title, "role_type": "product_engineer", "tags": ["product_engineer"],
            "department": None, "location": None, "employment_type": None, "url": f"https://j/{ext}", "posted_at": None}


def _e(ext, id_, is_open=True, first_seen="2026-11-01"):
    return {"id": id_, "external_id": ext, "is_open": is_open, "first_seen": first_seen}


def test_new_job_gets_first_seen_today():
    plan = jobs.plan_sync("acme", [], [_m("a")], 5, 0, TODAY)
    assert plan.inserted == 1 and plan.upserts[0]["first_seen"] == "2026-11-21" and plan.upserts[0]["is_open"]


def test_known_job_keeps_its_original_first_seen():
    plan = jobs.plan_sync("acme", [_e("a", 1)], [_m("a")], 5, 0, TODAY)
    assert plan.inserted == 0 and plan.upserts[0]["first_seen"] == "2026-11-01"
    assert plan.upserts[0]["last_seen"] == "2026-11-21" and plan.close_ids == []


def test_job_that_disappears_is_closed():
    plan = jobs.plan_sync("acme", [_e("a", 1), _e("b", 2)], [_m("a")], 5, 0, TODAY)
    assert plan.close_ids == [2] and not plan.held


def test_closed_job_that_returns_is_reopened():
    plan = jobs.plan_sync("acme", [_e("a", 1, is_open=False)], [_m("a")], 5, 0, TODAY)
    assert plan.reopened == 1 and plan.upserts[0]["is_open"] and plan.upserts[0]["closed_at"] is None


def test_all_matching_roles_filled_but_board_not_empty_closes_them():
    plan = jobs.plan_sync("acme", [_e("a", 1)], [], 8, 0, TODAY)
    assert plan.close_ids == [1] and not plan.held


def test_empty_board_is_held_for_one_run_then_closed():
    first = jobs.plan_sync("acme", [_e("a", 1)], [], 0, 0, TODAY)
    assert first.held and first.close_ids == [] and first.new_empty_streak == 1
    second = jobs.plan_sync("acme", [_e("a", 1)], [], 0, 1, TODAY)
    assert not second.held and second.close_ids == [1] and second.new_empty_streak == 0


def test_empty_board_with_nothing_open_is_not_held():
    plan = jobs.plan_sync("acme", [], [], 0, 0, TODAY)
    assert not plan.held and plan.new_empty_streak == 0


# ----------------------------------------------------------------------
# End to end through run() with an in-memory store and a fake job system
# ----------------------------------------------------------------------
NOW = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)


def _install_boards(monkeypatch, boards: dict):
    """boards: {(ats, slug): Board | Exception | None}"""
    def make(ats):
        def fetch(slug):
            val = boards.get((ats, slug))
            if isinstance(val, Exception):
                raise val
            return val
        return fetch
    for ats in jobs.FETCHERS:
        monkeypatch.setitem(jobs.FETCHERS, ats, make(ats))
    monkeypatch.setattr("time.sleep", lambda *_: None)


def _run(store, cfg, **kw):
    args = dict(dry_run=False, discover_only=False, only_key=None, limit=None, report_path=None)
    args.update(kw)
    return jobs.run(store, cfg, **args)


def test_end_to_end_discover_then_refresh_then_close(monkeypatch, cfg):
    postings = [Posting("a", "Product Engineer", url="https://j/a"), Posting("b", "Account Executive", url="https://j/b")]
    boards = {("ashby", "hanoverpark"): Board("ashby", "hanoverpark", None, postings, "Hanover Park builds AI")}
    _install_boards(monkeypatch, boards)
    store = FakeStore([{"company_key": "hanoverpark", "company": "Hanover Park"}])

    assert _run(store, cfg) == 0
    care = store.careers["hanoverpark"]
    assert care["status"] == "ok" and care["verified"] and care["careers_url"] == "https://jobs.ashbyhq.com/hanoverpark"
    assert [j["external_id"] for j in store.jobs] == ["a"] and store.jobs[0]["is_open"]

    # next day: the role is gone from a non-empty board -> closed
    boards[("ashby", "hanoverpark")] = Board("ashby", "hanoverpark", None, [Posting("b", "Account Executive", url="https://j/b")], "Hanover Park")
    assert _run(store, cfg) == 0
    assert store.jobs[0]["is_open"] is False and store.jobs[0]["closed_at"]


def test_dry_run_writes_nothing(monkeypatch, cfg):
    boards = {("ashby", "acme"): Board("ashby", "acme", None, [Posting("a", "AI Engineer", url="https://j/a")], "Acme builds")}
    _install_boards(monkeypatch, boards)
    store = FakeStore([{"company_key": "acme", "company": "Acme"}])
    assert _run(store, cfg, dry_run=True) == 0
    assert store.writes == 0 and store.jobs == [] and store.careers == {}


def test_failed_fetch_never_closes_jobs(monkeypatch, cfg):
    _install_boards(monkeypatch, {("ashby", "acme"): jobs.FetchError("HTTP 503")})
    existing_job = {"id": 1, "company_key": "acme", "external_id": "a", "title": "AI Engineer", "is_open": True,
                    "first_seen": "2026-11-01", "closed_at": None}
    care = {"company_key": "acme", "company": "Acme", "ats": "ashby", "ats_slug": "acme", "verified": True,
            "status": "ok", "source": "auto", "consecutive_errors": 0}
    store = FakeStore([{"company_key": "acme", "company": "Acme"}], {"acme": care}, [existing_job])
    rc = _run(store, cfg)
    assert store.jobs[0]["is_open"] is True                      # untouched
    assert store.careers["acme"]["status"] == "error" and store.careers["acme"]["consecutive_errors"] == 1
    assert rc == 1                                               # 100% of fetches failed -> non-zero exit


def test_manual_url_is_never_overwritten_by_discovery(monkeypatch, cfg):
    boards = {("ashby", "acme"): Board("ashby", "acme", None, [], "Acme text")}
    _install_boards(monkeypatch, boards)
    monkeypatch.setattr(jobs, "fetch_page", lambda url: "<html>nothing readable here</html>")
    care = {"company_key": "acme", "company": "Acme", "careers_url": "https://acme.com/careers", "ats": "custom",
            "ats_slug": None, "source": "manual", "verified": True, "status": "unsupported", "needs_refresh": True}
    store = FakeStore([{"company_key": "acme", "company": "Acme"}], {"acme": care})
    assert _run(store, cfg) == 0
    after = store.careers["acme"]
    assert after["careers_url"] == "https://acme.com/careers" and after["source"] == "manual" and after["ats"] == "custom"
    assert after["needs_refresh"] is False                       # flag cleared


def test_manual_supported_board_keeps_the_admins_url_but_fetches_jobs(monkeypatch, cfg):
    boards = {("greenhouse", "acme"): Board("greenhouse", "acme", "Acme", [Posting("9", "Forward Deployed Engineer", url="https://j/9")])}
    _install_boards(monkeypatch, boards)
    care = {"company_key": "acme", "company": "Acme", "careers_url": "https://boards.greenhouse.io/acme",
            "ats": "greenhouse", "ats_slug": "acme", "source": "manual", "verified": True, "status": "ok", "needs_refresh": True}
    store = FakeStore([{"company_key": "acme", "company": "Acme"}], {"acme": care})
    assert _run(store, cfg) == 0
    assert store.careers["acme"]["careers_url"] == "https://boards.greenhouse.io/acme"
    assert store.careers["acme"]["needs_refresh"] is False
    assert [j["role_type"] for j in store.jobs] == ["forward_deployed_engineer"]


def test_no_board_found_is_recorded_and_not_retried_until_due(monkeypatch, cfg):
    _install_boards(monkeypatch, {})
    store = FakeStore([{"company_key": "ghost", "company": "Ghost Co"}])
    assert _run(store, cfg) == 0
    assert store.careers["ghost"]["status"] == "no_board"
    writes = store.writes
    assert _run(store, cfg) == 0                                  # checked moments ago -> skipped
    assert store.writes == writes


def test_transient_errors_during_discovery_are_not_recorded_as_no_board(monkeypatch, cfg):
    _install_boards(monkeypatch, {("ashby", "ghostco"): jobs.FetchError("HTTP 429")})
    store = FakeStore([{"company_key": "ghost", "company": "Ghost Co"}])
    _run(store, cfg)
    assert store.careers["ghost"]["status"] == "error"            # will be retried next run


def test_unverified_guess_is_stored_but_flagged(monkeypatch, cfg):
    boards = {("ashby", "aqua"): Board("ashby", "aqua", None, [Posting("1", "AI Engineer", url="https://j/1")], "an unrelated firm")}
    _install_boards(monkeypatch, boards)
    store = FakeStore([{"company_key": "aqua", "company": "Aqua"}])
    _run(store, cfg)
    assert store.careers["aqua"]["verified"] is False and store.careers["aqua"]["status"] == "unverified"


def test_board_that_vanishes_is_forgotten_after_three_misses_but_jobs_stay(monkeypatch, cfg):
    _install_boards(monkeypatch, {})                              # every board now 404s
    job = {"id": 1, "company_key": "acme", "external_id": "a", "title": "AI Engineer", "is_open": True,
           "first_seen": "2026-11-01", "closed_at": None}
    care = {"company_key": "acme", "company": "Acme", "ats": "ashby", "ats_slug": "acme", "verified": True,
            "status": "ok", "source": "auto", "consecutive_errors": 0}
    store = FakeStore([{"company_key": "acme", "company": "Acme"}], {"acme": care}, [job])
    for expected in ("error", "error", "no_board"):
        _run(store, cfg)
        assert store.careers["acme"]["status"] == expected
    assert store.jobs[0]["is_open"] is True


def test_due_items_orders_needs_refresh_first_and_respects_retry_window(cfg):
    now = datetime(2026, 11, 21, tzinfo=timezone.utc)
    companies = [{"company_key": k, "company": k} for k in ("new", "fresh", "stale", "refresh", "known")]
    careers = {
        "fresh": {"status": "no_board", "last_checked": "2026-11-20T00:00:00+00:00"},
        "stale": {"status": "no_board", "last_checked": "2026-11-01T00:00:00+00:00"},
        "refresh": {"status": "ok", "ats": "ashby", "ats_slug": "r", "needs_refresh": True},
        "known": {"status": "ok", "ats": "ashby", "ats_slug": "k"},
    }
    keys = [i["company_key"] for i in jobs.due_items(companies, careers, 7, None, False, now)]
    assert keys[0] == "refresh" and set(keys) == {"refresh", "new", "stale", "known"}
    assert [i["company_key"] for i in jobs.due_items(companies, careers, 7, None, True, now)] == ["refresh", "new", "stale"]
    assert [i["company_key"] for i in jobs.due_items(companies, careers, 7, "fresh", False, now)] == ["fresh"]


def test_dry_run_tolerates_missing_tables_but_a_real_run_does_not(monkeypatch, cfg):
    _install_boards(monkeypatch, {})

    class NoTables(FakeStore):
        def careers_all(self):
            raise RuntimeError("{'code': 'PGRST205', 'message': \"Could not find the table 'public.company_careers'\"}")

    store = NoTables([{"company_key": "ghost", "company": "Ghost Co"}])
    assert _run(store, cfg, dry_run=True) == 0 and store.writes == 0
    with pytest.raises(RuntimeError):
        _run(store, cfg, dry_run=False)


# ----------------------------------------------------------------------
# Rate limiting (Workable returned HTTP 429 during the first full dry run)
# ----------------------------------------------------------------------
def test_host_limiter_spaces_requests(monkeypatch):
    sleeps = []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))
    lim = jobs.HostLimiter(interval=1.0)
    lim.acquire(); lim.acquire(); lim.acquire()
    # The first request goes straight through (no sleep). Because sleep is faked no time passes, so
    # the 2nd and 3rd requests are scheduled 1s and 2s out: one interval apart from each other.
    assert len(sleeps) == 2
    assert 0.95 < sleeps[0] < 1.05 and 0.95 < sleeps[1] - sleeps[0] < 1.05


def test_429_honours_retry_after_then_succeeds(monkeypatch):
    sleeps = []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))
    seq = iter([FakeResponse({}, 429, {"Retry-After": "7"}), FakeResponse({"ok": True}, 200)])
    monkeypatch.setattr(jobs.requests, "get", lambda *a, **k: next(seq))
    assert jobs._get("https://example.com/x").json() == {"ok": True}
    assert max(sleeps) >= 6.5                          # waited for the server's Retry-After


def test_repeated_429_raises_ratelimited_not_a_plain_error(monkeypatch):
    calls = []
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setattr(jobs.requests, "get", lambda *a, **k: (calls.append(1), FakeResponse({}, 429))[1])
    with pytest.raises(jobs.RateLimited):
        jobs._get("https://example.com/x")
    assert len(calls) == 4                              # attempts + one extra try for a 429


def test_host_is_paused_after_repeated_429s(monkeypatch):
    jobs.configure({"request_delay": 0, "host_min_interval": {}, "rate_limit_trip_after": 2, "rate_limit_cooldown": 300})
    calls = []
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setattr(jobs.requests, "get", lambda *a, **k: (calls.append(1), FakeResponse({}, 429))[1])
    for _ in range(2):
        with pytest.raises(jobs.RateLimited):
            jobs._get("https://example.com/x")
    made = len(calls)
    with pytest.raises(jobs.RateLimited):               # now paused: no request is even sent
        jobs._get("https://example.com/x")
    assert len(calls) == made


def test_a_successful_response_resets_the_429_streak(monkeypatch):
    jobs.configure({"request_delay": 0, "host_min_interval": {}, "rate_limit_trip_after": 2, "rate_limit_cooldown": 300})
    monkeypatch.setattr("time.sleep", lambda s: None)
    state = {"mode": "429"}
    monkeypatch.setattr(jobs.requests, "get", lambda *a, **k: FakeResponse({}, 429) if state["mode"] == "429" else FakeResponse({}, 404))
    with pytest.raises(jobs.RateLimited):
        jobs._get("https://example.com/x")
    state["mode"] = "ok"
    assert jobs._get("https://example.com/x") is None   # 404 = "no such board", and it clears the streak
    state["mode"] = "429"
    with pytest.raises(jobs.RateLimited):
        jobs._get("https://example.com/x")
    state["mode"] = "ok"
    assert jobs._get("https://example.com/x") is None   # host was never paused (streak never reached 2)


def test_discovery_throttled_only_on_workable_is_pending_not_an_error(monkeypatch, cfg):
    _install_boards(monkeypatch, {("workable", "ghostco"): jobs.RateLimited("HTTP 429"),
                                  ("workable", "ghost-co"): jobs.RateLimited("HTTP 429")})
    store = FakeStore([{"company_key": "ghost", "company": "Ghost Co"}])
    rc = _run(store, cfg)
    assert store.careers["ghost"]["status"] == "pending"
    assert store.careers["ghost"].get("consecutive_errors", 0) == 0
    assert rc == 0                                       # throttled lookups are not failures


def test_workable_throttling_does_not_hide_a_board_found_elsewhere(monkeypatch, cfg):
    boards = {("greenhouse", "acme"): Board("greenhouse", "acme", "Acme", [Posting("1", "AI Engineer", url="https://j/1")]),
              ("workable", "acme"): jobs.RateLimited("HTTP 429")}
    _install_boards(monkeypatch, boards)
    store = FakeStore([{"company_key": "acme", "company": "Acme"}])
    _run(store, cfg)
    assert store.careers["acme"]["status"] == "ok" and store.careers["acme"]["ats"] == "greenhouse"


def test_workable_is_asked_last_and_only_for_the_first_name_guesses(monkeypatch, cfg):
    order = []

    def make(ats):
        def fetch(slug):
            order.append((ats, slug))
            return None
        return fetch

    for ats in jobs.FETCHERS:
        monkeypatch.setitem(jobs.FETCHERS, ats, make(ats))
    monkeypatch.setattr("time.sleep", lambda *_: None)
    jobs.discover("Long Company Name Labs", {**cfg["jobs"], "workable_max_candidates": 2})
    first_workable = min(i for i, (ats, _) in enumerate(order) if ats == "workable")
    assert first_workable >= 12                                              # every guess tried on the other three first
    assert all(ats != "workable" for ats, _ in order[:first_workable])      # Ashby/Greenhouse/Lever first
    assert sum(1 for ats, _ in order if ats == "workable") == 2             # capped at two guesses
    assert sum(1 for ats, _ in order if ats == "ashby") > 2                 # the others try every guess


def test_discovery_with_a_real_failure_is_an_error_even_if_workable_was_also_throttled(monkeypatch, cfg):
    _install_boards(monkeypatch, {("ashby", "ghostco"): jobs.FetchError("HTTP 500"),
                                  ("workable", "ghostco"): jobs.RateLimited("HTTP 429")})
    store = FakeStore([{"company_key": "ghost", "company": "Ghost Co"}])
    _run(store, cfg)
    assert store.careers["ghost"]["status"] == "error"


def test_refresh_throttled_leaves_status_and_jobs_alone(monkeypatch, cfg):
    _install_boards(monkeypatch, {("workable", "acme"): jobs.RateLimited("HTTP 429")})
    job = {"id": 1, "company_key": "acme", "external_id": "a", "title": "AI Engineer", "is_open": True,
           "first_seen": "2026-11-01", "closed_at": None}
    care = {"company_key": "acme", "company": "Acme", "ats": "workable", "ats_slug": "acme", "verified": True,
            "status": "ok", "source": "auto", "consecutive_errors": 0}
    store = FakeStore([{"company_key": "acme", "company": "Acme"}], {"acme": care}, [job])
    rc = _run(store, cfg)
    assert store.jobs[0]["is_open"] is True
    assert store.careers["acme"]["status"] == "ok" and store.careers["acme"]["consecutive_errors"] == 0
    assert rc == 0


def test_long_retry_after_pauses_the_host_at_once_without_waiting(monkeypatch):
    sleeps, calls = [], []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(jobs.requests, "get",
                        lambda *a, **k: (calls.append(1), FakeResponse({}, 429, {"Retry-After": "85668"}))[1])
    with pytest.raises(jobs.RateLimited):
        jobs._get("https://example.com/x")
    assert len(calls) == 1                               # no retries against a host that said "come back tomorrow"
    assert sum(sleeps) < 5                               # and no long sleep
    with pytest.raises(jobs.RateLimited):                # the host stays paused for the rest of the run
        jobs._get("https://example.com/y")
    assert len(calls) == 1


def test_short_retry_after_is_still_waited_out(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    seq = iter([FakeResponse({}, 429, {"Retry-After": "30"}), FakeResponse({"ok": 1}, 200)])
    monkeypatch.setattr(jobs.requests, "get", lambda *a, **k: next(seq))
    assert jobs._get("https://example.com/x").json() == {"ok": 1}


# ----------------------------------------------------------------------
# Custom careers pages: (1) a job board linked on the page, (2) JobPosting JSON-LD
# ----------------------------------------------------------------------
def test_find_board_links_in_anchors_iframes_scripts_and_escaped_json():
    page = """
      <a href="https://jobs.ashbyhq.com/acme">Open roles</a>
      <iframe src="https://boards.greenhouse.io/embed/job_board?for=acmeco"></iframe>
      <script src="https://boards.greenhouse.io/embed/job_board/js?for=acmeco"></script>
      <script>var u = "https:\\/\\/jobs.lever.co\\/acme-lever";</script>
      <a href="https://apply.workable.com/acme-w/?a=1&amp;b=2">x</a>
    """
    got = jobs.find_board_links(page)
    assert got[0] == ("greenhouse", "acmeco")                     # mentioned twice: first
    assert set(got) == {("greenhouse", "acmeco"), ("ashby", "acme"), ("lever", "acme-lever"), ("workable", "acme-w")}


def test_find_board_links_ignores_vendor_marketing_links_and_non_board_paths():
    page = """<a href="https://www.ashbyhq.com/">Powered by Ashby</a> <a href="https://www.lever.co/product">Lever</a>
              <a href="https://www.greenhouse.io/">Greenhouse</a> <a href="https://boards.greenhouse.io/embed/job_board/js">js</a>
              <script src="https://jobs.ashbyhq.com/embed"></script>
              <p>We believe in leverage.</p>"""
    assert jobs.find_board_links(page) == []


def test_find_board_links_understands_api_urls():
    page = 'fetch("https://api.ashbyhq.com/posting-api/job-board/acme-ai")'
    assert jobs.find_board_links(page) == [("ashby", "acme-ai")]


def _ld(*objs):
    import json
    return "".join(f'<script type="application/ld+json">{json.dumps(o)}</script>' for o in objs)


def test_extract_job_postings_basic_fields_and_relative_url():
    page = _ld({"@context": "https://schema.org", "@type": "JobPosting", "title": "Forward Deployed Engineer",
                "identifier": {"@type": "PropertyValue", "value": "FDE-7"}, "url": "/careers/fde",
                "datePosted": "2026-11-01", "employmentType": ["FULL_TIME"],
                "jobLocation": {"@type": "Place", "address": {"addressLocality": "New York", "addressRegion": "NY", "addressCountry": "US"}}})
    (p,) = jobs.extract_job_postings(page, "https://acme.com/careers/")
    assert p.external_id == "ld:FDE-7" and p.title == "Forward Deployed Engineer"
    assert p.url == "https://acme.com/careers/fde" and p.location == "New York, NY, US"
    assert p.employment_type == "FULL_TIME" and p.posted_at.startswith("2026-11-01")


def test_extract_job_postings_graph_itemlist_and_type_lists():
    page = _ld(
        {"@graph": [{"@type": "WebPage"}, {"@type": "JobPosting", "title": "AI Engineer", "identifier": "1"}]},
        {"@type": "ItemList", "itemListElement": [{"@type": "ListItem", "item": {"@type": ["JobPosting"], "title": "Product Engineer", "identifier": "2"}}]},
        [{"@type": "JobPosting", "title": "Sales Lead", "identifier": "3"}],
    )
    assert sorted(p.title for p in jobs.extract_job_postings(page, "https://acme.com/c")) == ["AI Engineer", "Product Engineer", "Sales Lead"]


def test_extract_job_postings_skips_bad_json_expired_untitled_and_duplicates():
    page = ('<script type="application/ld+json">{not json</script>'
            + _ld({"@type": "JobPosting", "title": "Old Role", "identifier": "9", "validThrough": "2020-01-01"},
                  {"@type": "JobPosting", "identifier": "10"},
                  {"@type": "JobPosting", "title": "AI Engineer", "identifier": "11"},
                  {"@type": "JobPosting", "title": "AI Engineer", "identifier": "11"},
                  {"@type": "Organization", "name": "Acme"}))
    got = jobs.extract_job_postings(page, "https://acme.com/c")
    assert [(p.title, p.url) for p in got] == [("AI Engineer", "https://acme.com/c")]   # page URL when the posting has none


def test_extract_job_postings_ids_are_stable_without_an_identifier_and_remote_is_noted():
    node = {"@type": "JobPosting", "title": "AI Engineer", "url": "https://acme.com/j/1", "jobLocationType": "TELECOMMUTE"}
    a = jobs.extract_job_postings(_ld(node), "https://acme.com/c")[0]
    b = jobs.extract_job_postings(_ld(node), "https://acme.com/c")[0]
    assert a.external_id == b.external_id and a.external_id.startswith("ld:") and a.location == "Remote"


def test_assert_public_refuses_private_and_loopback_hosts(real_assert_public):
    for host in ("127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "localhost"):
        with pytest.raises(jobs.FetchError):
            real_assert_public(host)
    real_assert_public("8.8.8.8")                                 # a public address passes


class _Page(FakeResponse):
    def __init__(self, text, status=200, ctype="text/html; charset=utf-8", url="https://acme.com/careers"):
        super().__init__(None, status, {"Content-Type": ctype})
        self.text, self.url = text, url


def _fake_site(monkeypatch, pages: dict):
    """pages: {url: _Page | None | Exception}; any other URL is a 404."""
    def fake_get(url, headers=None, params=None, timeout=None):
        val = pages.get(url)
        if isinstance(val, Exception):
            raise val
        return val if val is not None else FakeResponse({}, 404)
    monkeypatch.setattr(jobs.requests, "get", fake_get)
    monkeypatch.setattr("time.sleep", lambda *_: None)


def test_fetch_page_returns_html(monkeypatch):
    _fake_site(monkeypatch, {"https://acme.com/robots.txt": _Page("User-agent: *\nAllow: /", ctype="text/plain"),
                             "https://acme.com/careers": _Page("<html>hi</html>")})
    assert jobs.fetch_page("https://acme.com/careers") == "<html>hi</html>"


def test_fetch_page_respects_robots_txt(monkeypatch):
    _fake_site(monkeypatch, {"https://acme.com/robots.txt": _Page("User-agent: *\nDisallow: /careers", ctype="text/plain"),
                             "https://acme.com/careers": _Page("<html>secret</html>")})
    assert jobs.fetch_page("https://acme.com/careers") is None


def test_fetch_page_missing_robots_is_fine_but_missing_page_is_an_error(monkeypatch):
    _fake_site(monkeypatch, {})
    with pytest.raises(jobs.FetchError):
        jobs.fetch_page("https://acme.com/careers")


def test_fetch_page_ignores_non_html(monkeypatch):
    _fake_site(monkeypatch, {"https://acme.com/robots.txt": None,
                             "https://acme.com/careers": _Page("%PDF", ctype="application/pdf")})
    assert jobs.fetch_page("https://acme.com/careers") is None


def test_fetch_page_refuses_a_redirect_to_a_private_host(monkeypatch, real_assert_public):
    _fake_site(monkeypatch, {"https://acme.com/robots.txt": None,
                             "https://acme.com/careers": _Page("<html>internal</html>", url="http://127.0.0.1/admin")})
    monkeypatch.setattr(jobs, "_assert_public", real_assert_public)      # real check; acme.com is faked as resolvable below
    real = real_assert_public
    monkeypatch.setattr(jobs, "_assert_public", lambda host: None if host == "acme.com" else real(host))
    with pytest.raises(jobs.FetchError):
        jobs.fetch_page("https://acme.com/careers")


CUSTOM = {"company_key": "acme", "company": "Acme", "careers_url": "https://acme.com/careers", "ats": "custom",
          "ats_slug": None, "source": "manual", "verified": True, "status": "unsupported", "needs_refresh": True}


def _custom_store(**care):
    return FakeStore([{"company_key": "acme", "company": "Acme"}], {"acme": {**CUSTOM, **care}})


def test_custom_page_with_a_linked_board_switches_to_that_board_and_keeps_the_admins_url(monkeypatch, cfg):
    board = Board("ashby", "acme", "Acme", [Posting("1", "Forward Deployed Engineer", url="https://j/1"),
                                             Posting("2", "Account Executive", url="https://j/2")])
    _install_boards(monkeypatch, {("ashby", "acme"): board})
    monkeypatch.setattr(jobs, "fetch_page", lambda url: '<a href="https://jobs.ashbyhq.com/acme">Jobs</a>')
    store = _custom_store()
    assert _run(store, cfg) == 0
    care = store.careers["acme"]
    assert (care["ats"], care["ats_slug"], care["status"], care["source"]) == ("ashby", "acme", "ok", "manual")
    assert care["careers_url"] == "https://acme.com/careers" and care["needs_refresh"] is False
    assert [j["external_id"] for j in store.jobs] == ["1"]
    # next day it is an ordinary manual board: refreshed through the API, the page is not read again
    monkeypatch.setattr(jobs, "fetch_page", lambda url: pytest.fail("page should not be read again"))
    assert _run(store, cfg) == 0


def test_custom_page_with_jobposting_data_syncs_roles_and_closes_them_when_they_disappear(monkeypatch, cfg):
    _install_boards(monkeypatch, {})
    page = {"html": _ld({"@type": "JobPosting", "title": "Product Engineer", "identifier": "p1", "url": "https://acme.com/j/p1"},
                        {"@type": "JobPosting", "title": "Office Manager", "identifier": "p2"})}
    monkeypatch.setattr(jobs, "fetch_page", lambda url: page["html"])
    store = _custom_store()
    assert _run(store, cfg) == 0
    care = store.careers["acme"]
    assert (care["ats"], care["status"], care["verified"]) == ("custom", "ok", True)
    assert [(j["external_id"], j["is_open"]) for j in store.jobs] == [("ld:p1", True)]       # Office Manager is not a match
    assert _run(store, cfg) == 0                                                               # status ok: refreshed daily
    page["html"] = "<html>We are not hiring right now</html>"                                  # roles vanish from the page
    _run(store, cfg)
    assert store.jobs[0]["is_open"]                                                            # held for one run
    _run(store, cfg)
    assert not store.jobs[0]["is_open"]                                                        # then closed


def test_custom_page_with_nothing_readable_is_unsupported_and_lists_no_roles(monkeypatch, cfg):
    _install_boards(monkeypatch, {})
    monkeypatch.setattr(jobs, "fetch_page", lambda url: "<html><h2>What you'll do</h2></html>")
    store = _custom_store()
    assert _run(store, cfg) == 0
    care = store.careers["acme"]
    assert care["status"] == "unsupported" and care["ats"] == "custom" and care["careers_url"] == "https://acme.com/careers"
    assert store.jobs == []


def test_replacing_a_board_url_with_an_unreadable_page_retires_the_old_boards_roles(monkeypatch, cfg):
    _install_boards(monkeypatch, {})
    monkeypatch.setattr(jobs, "fetch_page", lambda url: "<html>nothing</html>")
    old = [{"id": 1, "company_key": "acme", "external_id": "x", "title": "AI Engineer", "role_type": "ai_engineer", "tags": ["ai_engineer"],
            "url": "https://j/x", "first_seen": "2026-11-01", "last_seen": "2026-11-20", "is_open": True, "closed_at": None}]
    store = FakeStore([{"company_key": "acme", "company": "Acme"}], {"acme": {**CUSTOM}}, old)
    _run(store, cfg)
    assert store.jobs[0]["is_open"]                                                            # held one run
    store.careers["acme"]["needs_refresh"] = True
    _run(store, cfg)
    assert not store.jobs[0]["is_open"]


def test_custom_page_that_cannot_be_read_never_closes_roles(monkeypatch, cfg):
    _install_boards(monkeypatch, {})
    def boom(url):
        raise jobs.FetchError("HTTP 403")
    monkeypatch.setattr(jobs, "fetch_page", boom)
    old = [{"id": 1, "company_key": "acme", "external_id": "ld:1", "title": "AI Engineer", "role_type": "ai_engineer", "tags": ["ai_engineer"],
            "url": "https://j/1", "first_seen": "2026-11-01", "last_seen": "2026-11-20", "is_open": True, "closed_at": None}]
    store = FakeStore([{"company_key": "acme", "company": "Acme"}], {"acme": {**CUSTOM, "status": "ok"}}, old)
    _run(store, cfg)
    _run(store, cfg)
    _run(store, cfg)
    assert store.jobs[0]["is_open"]
    assert store.careers["acme"]["status"] == "error" and store.careers["acme"]["consecutive_errors"] == 3


def test_custom_page_throttled_is_not_an_error(monkeypatch, cfg):
    _install_boards(monkeypatch, {})
    def limited(url):
        raise jobs.RateLimited("HTTP 429")
    monkeypatch.setattr(jobs, "fetch_page", limited)
    store = _custom_store()
    assert _run(store, cfg) == 0
    assert store.careers["acme"].get("consecutive_errors", 0) == 0


def test_due_rules_for_custom_pages(cfg):
    now = datetime(2026, 11, 21, tzinfo=timezone.utc)
    mk = lambda key, **kw: (key, {**CUSTOM, "company_key": key, "needs_refresh": False, **kw})
    careers = dict([
        mk("readable", status="ok", last_checked="2026-11-20T00:00:00+00:00"),       # daily
        mk("failing", status="error", last_checked="2026-11-20T00:00:00+00:00"),     # daily
        mk("fresh_unsupported", last_checked="2026-11-19T00:00:00+00:00"),           # looked 2 days ago: not yet
        mk("old_unsupported", last_checked="2026-11-01T00:00:00+00:00"),             # 20 days ago: look again
        mk("edited", needs_refresh=True, last_checked="2026-11-20T00:00:00+00:00"),
    ])
    companies = [{"company_key": k, "company": k} for k in careers]
    due = {i["company_key"] for i in jobs.due_items(companies, careers, 7, None, False, now)}
    assert due == {"readable", "failing", "old_unsupported", "edited"}


# ----------------------------------------------------------------------
# Companies added on the Jobs page (standalone: not in the blotter)
# ----------------------------------------------------------------------
def test_company_key_examples_shared_with_the_typescript_version():
    from src.store import company_key
    for name, key in fixture("company_keys.json").items():
        assert company_key(name) == key, name


def test_standalone_companies_are_manual_rows_without_a_deal():
    deals = [{"company_key": "inblotter", "company": "In Blotter"}]
    careers = {
        "inblotter": {"source": "manual", "company": "In Blotter"},         # has a deal: normal company
        "added": {"source": "manual", "company": "Added Co"},               # standalone
        "stale": {"source": "auto", "company": "Deleted Deal Co"},          # deal removed long ago: ignored
    }
    assert jobs.standalone_companies(deals, careers) == [{"company_key": "added", "company": "Added Co"}]


def test_run_checks_a_standalone_company_and_its_roles_appear(monkeypatch, cfg):
    board = Board("ashby", "addedco", "Added Co", [Posting("1", "Forward Deployed Engineer", url="https://j/1")])
    _install_boards(monkeypatch, {("ashby", "addedco"): board})
    care = {"company_key": "addedco", "company": "Added Co", "careers_url": "https://jobs.ashbyhq.com/addedco", "ats": "ashby",
            "ats_slug": "addedco", "source": "manual", "verified": True, "status": "ok", "needs_refresh": True}
    store = FakeStore([{"company_key": "other", "company": "Other"}], {"addedco": care})
    _install_boards(monkeypatch, {("ashby", "addedco"): board})        # 'other' has no board: stays unknown
    assert _run(store, cfg) == 0
    assert [(j["company_key"], j["external_id"]) for j in store.jobs] == [("addedco", "1")]
    assert store.careers["addedco"]["needs_refresh"] is False
    assert store.careers["addedco"]["careers_url"] == "https://jobs.ashbyhq.com/addedco"      # the admin's URL is kept


def test_standalone_custom_page_is_read_like_any_manual_page(monkeypatch, cfg):
    _install_boards(monkeypatch, {})
    monkeypatch.setattr(jobs, "fetch_page", lambda url: _ld({"@type": "JobPosting", "title": "AI Engineer", "identifier": "a1"}))
    care = {**CUSTOM, "company_key": "addedco", "company": "Added Co"}
    store = FakeStore([], {"addedco": care})
    assert _run(store, cfg) == 0
    assert [j["external_id"] for j in store.jobs] == ["ld:a1"]
