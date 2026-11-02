"""GitHub and X: disabled without tokens; fetch + extract on synthetic fixtures (fake logins)."""

from __future__ import annotations

import json

import httpx
import pytest

from rebox_worker.connectors import REGISTRY, get_connector, is_enabled
from rebox_worker.connectors.github import GitHub, org_of
from rebox_worker.connectors.x import X, expanded_url
from rebox_worker.models import CursorUpdate, RawRecord

from .conftest import ctx, facts, fixture_json

# ── tokens ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("name", "var"), [("github", "GITHUB_TOKEN"), ("x", "X_BEARER_TOKEN")])
def test_disabled_without_token(name, var, monkeypatch):
    monkeypatch.delenv(var, raising=False)
    conn = get_connector(name)
    assert not is_enabled(conn, ctx(env={}))
    with pytest.raises(RuntimeError, match=var):
        next(iter(conn.fetch(ctx(env={}), None)))
    assert is_enabled(conn, ctx(env={var: "t"}))


def test_schedule_due_skips_disabled(monkeypatch):
    """jobs.schedule_due builds Ctx() without env, so enabled() must also see os.environ."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    from rebox_worker.connectors.base import Ctx

    assert not is_enabled(GitHub(), Ctx())
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    assert is_enabled(GitHub(), Ctx())


def test_registry_has_all_connectors():
    assert {"wikidata", "hn", "github", "x"} <= set(REGISTRY)


# ── GitHub ────────────────────────────────────────────────────────────────


def _github_api():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        assert request.headers["Authorization"] == "Bearer fake-token"
        path = request.url.path
        if path == "/search/users":
            q = request.url.params["q"]
            rng = q.split("followers:")[1].split()[0]
            try:
                return httpx.Response(200, json=fixture_json(f"github/search/{rng}.json"))
            except FileNotFoundError:
                return httpx.Response(200, json={"total_count": 0, "items": []})
        kind, login = path.strip("/").split("/")
        try:
            return httpx.Response(200, json=fixture_json(f"github/{kind}/{login.lower()}.json"))
        except FileNotFoundError:
            return httpx.Response(404, json={"message": "Not Found"})

    return handler, calls


def _github_records(**kw):
    handler, calls = _github_api()
    conn = GitHub(transport=httpx.MockTransport(handler), sleep=lambda s: None)
    items = list(conn.fetch(ctx(env={"GITHUB_TOKEN": "fake-token"}, **kw), kw.pop("cursor", None)))
    return items, calls


def test_github_fetch_dedupes_and_fetches_orgs_once():
    items, calls = _github_records()
    recs = [i for i in items if isinstance(i, RawRecord)]
    assert [r.external_id for r in recs] == [f"user:example-octo-{i}" for i in range(1, 6)]
    org_calls = [c for c in calls if "/orgs/" in c]
    assert sorted(c.rsplit("/", 1)[1].lower() for c in org_calls) == [
        "example-ghost",
        "example-org-1",
        "example-org-2",
    ]  # example-org-1 fetched once though two users name it; the ghost 404s
    assert items[-1].cursor == {"last_login": None, "last_run_users": 5}
    assert all("_fixture" not in json.dumps(r.payload) for r in recs)  # only kept fields stored


def test_github_cursor_resumes_and_limit_stops():
    items, _ = _github_records(limit=2)
    recs = [i for i in items if isinstance(i, RawRecord)]
    assert len(recs) == 2 and items[-1] == CursorUpdate(cursor={"last_login": "example-octo-2"})
    handler, _ = _github_api()
    conn = GitHub(transport=httpx.MockTransport(handler), sleep=lambda s: None)
    rest = list(conn.fetch(ctx(env={"GITHUB_TOKEN": "fake-token"}), {"last_login": "example-octo-2"}))
    assert [r.external_id for r in rest if isinstance(r, RawRecord)] == [
        "user:example-octo-3",
        "user:example-octo-4",
        "user:example-octo-5",
    ]


def test_github_extract_person_and_org():
    items, _ = _github_records()
    recs = {i.external_id: i for i in items if isinstance(i, RawRecord)}
    c1 = GitHub().extract(recs["user:example-octo-1"])
    f = facts(c1)
    assert {
        ("person", "Example Octo One", "github", "example-octo-1"),
        ("person", "Example Octo One", "x_handle", "example_octo_1"),
        ("person", "Example Octo One", "website", "https://example-octo-1.example"),
        ("person", "Example Octo One", "headline", "Building example things at Example Org One."),
        ("person", "Example Octo One", "location", "San Francisco, CA"),
        ("company", "Example Org One", "github", "example-org-1"),
        ("company", "Example Org One", "domain", "example-org-1.example"),
        ("company", "Example Org One", "x_handle", "example_org_1"),
        ("person", "Example Octo One", "worked_at", "Example Org One"),
    } <= f
    edge = next(c for c in c1 if c.predicate == "worked_at")
    assert edge.valid_from is None and edge.value is None
    assert edge.object.identity == {
        "github": "example-org-1",
        "domain": "example-org-1.example",
        "x_handle": "example_org_1",
    }
    assert edge.subject.context and edge.subject.context[0].identity["github"] == "example-org-1"

    # "@Example-Org-1" (other case) -> same org refs and the same org source_refs.
    c2 = GitHub().extract(recs["user:example-octo-2"])
    assert ("person", "Example Octo Two", "worked_at", "Example Org One") in facts(c2)
    org_refs = lambda cs: {c.source_ref for c in cs if c.source_ref.startswith("github:org:")}  # noqa: E731
    assert org_refs(c1) == org_refs(c2)


def test_github_plain_company_and_missing_org_give_no_edge():
    items, _ = _github_records()
    recs = {i.external_id: i for i in items if isinstance(i, RawRecord)}
    c3 = GitHub().extract(recs["user:example-octo-3"])  # "Example Plain Co"
    c4 = GitHub().extract(recs["user:example-octo-4"])  # "@example-ghost" -> 404
    for cs in (c3, c4):
        assert not [c for c in cs if c.predicate == "worked_at" or c.subject.kind == "company"]
    assert ("person", "Example Octo Three", "x_handle", "example_octo_3") in facts(c3)
    assert not [c for c in c4 if c.predicate in ("website", "x_handle")]  # junk blog / handle dropped
    c5 = GitHub().extract(recs["user:example-octo-5"])  # no display name; org website on github.com
    assert ("person", "example-octo-5", "name", "example-octo-5") in facts(c5)
    assert ("company", "example-org-2", "github", "example-org-2") in facts(c5)
    assert ("person", "example-octo-5", "worked_at", "example-org-2") in facts(c5)
    assert not [c for c in c5 if c.predicate == "domain"]  # github.com is not the org's domain


@pytest.mark.parametrize(
    ("company", "org"),
    [("@acme", "acme"), (" @Acme-Corp ", "Acme-Corp"), ("Acme Inc", None), ("@a @b", None), (None, None)],
)
def test_org_of(company, org):
    assert org_of(company) == org


def test_github_rate_limit_waits_for_reset():
    slept: list[float] = []
    seq = [httpx.Response(403, headers={"x-ratelimit-remaining": "0", "Retry-After": "7"})]

    def handler(request):
        return seq.pop(0) if seq else httpx.Response(200, json={"total_count": 0, "items": []})

    conn = GitHub(transport=httpx.MockTransport(handler), sleep=slept.append, min_interval_s=1.0)
    assert [i for i in conn.fetch(ctx(env={"GITHUB_TOKEN": "t"}), None) if isinstance(i, RawRecord)] == []
    assert 7.0 in slept


# ── X ─────────────────────────────────────────────────────────────────────


def _x_api(responses=None):
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer fake-token"
        params = dict(request.url.params)
        calls.append(params)
        if responses:
            return responses.pop(0)
        data = fixture_json("x/users_by.json")
        wanted = set(params["usernames"].split(","))
        data["data"] = [u for u in data["data"] if u["username"].lower() in wanted]
        return httpx.Response(200, json=data)

    return handler, calls


def _graph(_ctx):
    return fixture_json("x/graph_handles.json")["handles"]


def test_x_fetch_reads_graph_handles_and_batches():
    handler, calls = _x_api()
    conn = X(transport=httpx.MockTransport(handler), sleep=lambda s: None, handles=_graph)
    items = list(conn.fetch(ctx(env={"X_BEARER_TOKEN": "fake-token"}), None))
    recs = [i for i in items if isinstance(i, RawRecord)]
    assert [r.external_id for r in recs] == [
        "user:example_octo_1",
        "user:example_org_1",
        "user:example_x_only",
    ]
    assert calls[0]["usernames"] == "example_missing,example_octo_1,example_org_1,example_x_only"
    assert calls[0]["user.fields"] == "name,description,location,url,public_metrics,entities"
    assert items[-1].cursor == {"last_handle": None, "last_run_profiles": 3}


def test_x_batches_of_100_and_resume(monkeypatch):
    handles = {f"example_h{i:03d}": [{"kind": "person", "named": True}] for i in range(250)}
    handler, calls = _x_api()
    conn = X(transport=httpx.MockTransport(handler), sleep=lambda s: None, handles=lambda c: handles)
    items = list(conn.fetch(ctx(env={"X_BEARER_TOKEN": "fake-token"}), {"last_handle": "example_h049"}))
    assert [len(c["usernames"].split(",")) for c in calls] == [100, 100]
    cursors = [i.cursor for i in items if isinstance(i, CursorUpdate)]
    assert cursors[0] == {"last_handle": "example_h149"} and cursors[1] == {"last_handle": "example_h249"}


def test_x_extract_enriches_existing_entities():
    handler, _ = _x_api()
    conn = X(transport=httpx.MockTransport(handler), sleep=lambda s: None, handles=_graph)
    recs = {
        i.external_id: i
        for i in conn.fetch(ctx(env={"X_BEARER_TOKEN": "fake-token"}), None)
        if isinstance(i, RawRecord)
    }
    person = X().extract(recs["user:example_octo_1"])
    assert {(c.predicate, c.value) for c in person} == {
        ("x_followers", 1234),
        ("headline", "Synthetic person. Builds examples."),
        ("location", "San Francisco, CA"),
        ("website", "https://example-octo-1.example"),  # expanded, not t.co
    }  # no name: the entity already has one
    assert all(
        c.subject.identity == {"x_handle": "example_octo_1"} and c.subject.kind == "person" for c in person
    )
    company = X().extract(recs["user:example_org_1"])
    assert {c.predicate for c in company} == {"x_followers", "description", "website"}  # no location
    nameless = X().extract(recs["user:example_x_only"])
    assert ("name", "Example X Only") in {(c.predicate, c.value) for c in nameless}
    assert not [c for c in nameless if c.predicate == "website"]  # only a t.co link: dropped
    refs = [c.source_ref for c in person + company + nameless]
    assert len(refs) == len(set(refs)) and all(r.startswith("x:") for r in refs)


def test_x_rate_limit_429_waits_for_reset():
    import time as _t

    slept: list[float] = []
    reset = str(int(_t.time()) + 30)
    handler, _ = _x_api(
        [httpx.Response(429, headers={"x-rate-limit-reset": reset}), httpx.Response(200, json={})]
    )
    conn = X(transport=httpx.MockTransport(handler), sleep=slept.append, min_interval_s=1.0, handles=_graph)
    list(conn.fetch(ctx(env={"X_BEARER_TOKEN": "fake-token"}), None))
    assert any(25 <= s <= 32 for s in slept), slept


def test_expanded_url():
    assert (
        expanded_url(
            {
                "url": "https://t.co/a",
                "entities": {
                    "url": {"urls": [{"url": "https://t.co/a", "expanded_url": "https://x.example"}]}
                },
            }
        )
        == "https://x.example"
    )  # noqa: E501
    assert expanded_url({"url": "https://t.co/a"}) is None
    assert expanded_url({}) is None
