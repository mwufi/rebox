"""Hacker News launches: title parsing, company identity, extraction, fetch."""
# ruff: noqa: E501  (the title table keeps one real title per line)

from __future__ import annotations

import json

import httpx
import pytest

from rebox_worker.connectors.hn import (
    Hn,
    company_identity,
    name_matches_host,
    parse_title,
    site_domain,
)
from rebox_worker.models import CursorUpdate, RawRecord

from .conftest import ctx, facts, record, records

# ── title parsing: real title shapes from the Algolia API ──────────────────

TITLES = [
    # standard: name, batch, en dash
    ("Launch HN: Brex (YC W17) – Corporate Credit Card for Startups", ("Brex", "W17", "Corporate Credit Card for Startups")),
    ("Launch HN: Simantic (YC F26) – Firmware Simulation for AI Agents", ("Simantic", "F26", "Firmware Simulation for AI Agents")),
    ("Launch HN: Nao Labs (YC X25) – Cursor for Data", ("Nao Labs", "X25", "Cursor for Data")),
    ("Launch HN: Tinfoil (YC P25): Verifiable Privacy for Cloud AI", ("Tinfoil", "P25", "Verifiable Privacy for Cloud AI")),
    # multi-word and dotted names
    ("Launch HN: Buy Me a Coffee (YC W19) – Give your audience a way to thank you", ("Buy Me a Coffee", "W19", "Give your audience a way to thank you")),
    ("Launch HN: Fly.io (YC W20) – Deploy app servers close to your users", ("Fly.io", "W20", "Deploy app servers close to your users")),
    # colon / comma / no separator after the batch
    ("Launch HN: Substack (YC W18): Paid email newsletters made simple", ("Substack", "W18", "Paid email newsletters made simple")),
    ("Launch HN: Feather (YC S17), painless furniture rental for people who move a lot", ("Feather", "S17", "painless furniture rental for people who move a lot")),
    ("Launch HN: Miyagi (YC W25) turns YouTube videos into online, interactive courses", ("Miyagi", "W25", "turns YouTube videos into online, interactive courses")),
    # batch variants
    ("Launch HN: Koko (YC W22 Nonprofit) – Online Suicide Prevention Kit", ("Koko", "W22", "Online Suicide Prevention Kit")),
    ("Launch HN: Upsolve (YC Nonprofit W19) – TurboTax for Bankruptcy", ("Upsolve", "W19", "TurboTax for Bankruptcy")),
    ("Launch HN: Numericcal (YC.S18) – Lifecycle Management for ML Models on the Edge", ("Numericcal", "S18", "Lifecycle Management for ML Models on the Edge")),
    ("Launch HN: Effective Altruism Funds (YC W17 Nonprofit)", ("Effective Altruism Funds", "W17", None)),
    ("Launch HN: Meru.com (YC W21)", ("Meru.com", "W21", None)),
    # parentheses without a YC batch, but a separator: a name, no batch
    ("Launch HN: Remoteworkly (Not YC Yet) – Async Video Meetings for Remote Teams", ("Remoteworkly", None, "Async Video Meetings for Remote Teams")),
    ("Launch HN: Zero Carbon (SS2019) – Water Refill Station", ("Zero Carbon", None, "Water Refill Station")),
    ("Launch HN: Leadgen.io (Mom's Basement S20) – A Lead Generation Marketplace", ("Leadgen.io", None, "A Lead Generation Marketplace")),
    # no batch at all
    ("Launch HN: Freestyle – Sandboxes for Coding Agents", ("Freestyle", None, "Sandboxes for Coding Agents")),
    ("Launch HN: Neuraan - The NLP API specialized in the Spanish language", ("Neuraan", None, "The NLP API specialized in the Spanish language")),
    ("Launch HN: Tardigrade–Decentralized Cloud Storage with Client-Side Encryption", ("Tardigrade", None, "Decentralized Cloud Storage with Client-Side Encryption")),
    ("Launch HN: Direktiv: Event-Driven Serverless Container Workflows", ("Direktiv", None, "Event-Driven Serverless Container Workflows")),
    ("Launch HN: Browserless. Hosted headless Chrome", ("Browserless", None, "Hosted headless Chrome")),
    ("Launch HN: Calenvy.com – scheduling powered by email (like I Want Sandy)", ("Calenvy.com", None, "scheduling powered by email (like I Want Sandy)")),
    ("Launch HN: [Dx29.ai] Let's reinvent together the diagnosis of rare diseases", ("Dx29.ai", None, "Let's reinvent together the diagnosis of rare diseases")),
    ("Launch HN: FairFunders", ("FairFunders", None, None)),
    # "The ..." names, double spaces, prefix variants
    ("Launch HN: The Juggernaut (YC W19) – A Publication for the South Asian Diaspora", ("The Juggernaut", "W19", "A Publication for the South Asian Diaspora")),
    ("Launch HN: The  Lobby (YC W18) – 1-on-1 calls with company insiders to get hired", ("The Lobby", "W18", "1-on-1 calls with company insiders to get hired")),
    ("Launch  HN: Empromptu.ai – Agentic AI Building AI Apps", ("Empromptu.ai", None, "Agentic AI Building AI Apps")),
    ("(Re)launch HN: Weweb.io (YC W21) – visually build the front-end of your web-apps", ("Weweb.io", "W21", "visually build the front-end of your web-apps")),
    ("(Pre)Launch HN: Unthinkable – A visual travel guide to the edge of human thought", ("Unthinkable", None, "A visual travel guide to the edge of human thought")),
    ("Launch HN: Shimmer &amp; Co (YC S21) – ADHD coaching", ("Shimmer & Co", "S21", "ADHD coaching")),
]  # fmt: skip

NOT_LAUNCHES = [
    "Ask HN: Can you do a Launch HN without being a YC startup?",
    "Lessons learned from launch HN as an open source project",
    "Show HN: Inri – Digital KYC, Investing and Tax for Indian Expats (YC W23)",
    "Tell HN: Launch HN Test",
    "Launch HN: Prompt Engineering as a Service",  # a description, no name
    "Launch HN: A directory for keyboard shortcuts and hidden features",
    "Launch HN: I built a free RSS-to-email service",
    "Launch HN: Algofi (YC S21) Lofty (YC S19) Crypto as Collateral to Buy Property",  # two companies
    "Launch HN: [name-redacted] (YC [year-redacted]) – Price A/B Testing for Shopify",
    "Launch HN: Synbio School (Synthetic Biology)",
    "Launch HN Healbe GoBe 2 (automatic calorie intake tracker) on ProductHunt",
    "Launch HN: Exams, tasks, K8, eCommerce, cell sites, health, data quality, travel",
    "MVP Launch HN: Neo – EdTech Company Marrying Neuroscience with Entrepreneurship",
    "",
]


@pytest.mark.parametrize(("title", "expected"), TITLES, ids=[t[:48] for t, _ in TITLES])
def test_parse_title(title, expected):
    p = parse_title(title)
    assert p is not None, title
    assert (p.name, p.batch, p.tagline) == expected


@pytest.mark.parametrize("title", NOT_LAUNCHES)
def test_titles_that_are_not_a_named_launch(title):
    assert parse_title(title) is None


def test_title_table_is_big_enough():
    assert len(TITLES) >= 15 and sum(1 for _, e in TITLES if e[1] is None) >= 5


# ── company domain / GitHub owner ──────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://resend.com", "resend.com"),
        ("https://www.hyperprobe.co", "hyperprobe.co"),
        ("https://app.acme.ai/login?x=1", "acme.ai"),
        ("https://cloud.langfuse.com", "langfuse.com"),
        ("https://acme.co.uk/", "acme.co.uk"),
        ("https://github.com/browser-use/browser-use", None),
        ("https://www.youtube.com/watch?v=abc", None),
        ("https://optimumz.github.io/leadgen/", None),
        ("https://www.producthunt.com/posts/x", None),
        ("https://medium.com/@acme/launch", None),
        ("", None),
        (None, None),
        ("not a url", None),
    ],
)
def test_site_domain(url, expected):
    assert site_domain(url) == expected


@pytest.mark.parametrize(
    ("name", "host", "ok"),
    [
        ("Nao Labs", "getnao.io", True),
        ("Buy Me a Coffee", "buymeacoffee.com", True),
        ("Fly.io", "fly.io", True),
        ("Langfuse", "langfuse.com", True),
        ("Mito", "trymito.io", True),
        ("Paragon", "useparagon.com", True),
        ("Xix.ai", "x.com", False),
        ("Brex", "stripe.com", False),
        ("Rowboat", "anthropic.com", False),
        ("Cord", "encord.com", False),
    ],
)
def test_name_matches_host(name, host, ok):
    assert name_matches_host(name, host) is ok


def test_company_identity_prefers_story_url_then_matching_post_link():
    text = (
        'Hi HN! Demo: <a href="https:&#x2F;&#x2F;www.youtube.com&#x2F;watch?v=1">video</a>. '
        'We use <a href="https://stripe.com">Stripe</a>. Try it: <a href="https://app.getnao.io/signup">here</a>'
        " and the code: https://github.com/getnao/nao."
    )
    assert company_identity("Nao Labs", None, text) == ("getnao.io", "getnao")
    # A story URL on the company's site wins, even if the post links elsewhere.
    assert company_identity("Nao Labs", "https://nao.dev", text)[0] == "nao.dev"
    # A GitHub story URL gives the owner, never github.com as the domain.
    assert company_identity("Browser Use", "https://github.com/browser-use/browser-use", None) == (
        None,
        "browser-use",
    )
    # Links that don't match the name are not the company's domain.
    assert company_identity("Brex", None, '<a href="https://stripe.com">x</a>') == (None, None)


# ── extraction from recorded stories ───────────────────────────────────────


def test_extract_standard_launch():
    claims = Hn().extract(record("hn", "story:36309120"))  # Resend (YC W23), story URL resend.com
    f = facts(claims)
    assert ("company", "Resend", "name", "Resend") in f
    assert ("company", "Resend", "domain", "resend.com") in f
    assert ("company", "Resend", "yc_batch", "W23") in f
    assert ("company", "Resend", "launched", "Email API for developers using React") in f
    assert ("launch", "Email API for developers using React", "hn_story", "36309120") in f
    assert ("launch", "Email API for developers using React", "url", "https://resend.com") in f
    author = next(c for c in claims if c.predicate == "hn_user")
    login = author.value
    assert ("person", login, "posted", "Email API for developers using React") in f
    assert ("person", login, "founded", "Resend") in f
    founded = next(c for c in claims if c.predicate == "founded")
    assert founded.value == {"basis": "launch_hn_author"}
    # Every launch fact is dated by the story.
    when = record("hn", "story:36309120").payload["created_at"]
    assert {c.valid_from for c in claims if c.subject.kind == "launch"} == {when}
    points = next(c for c in claims if c.predicate == "points")
    assert isinstance(points.value, int)


def test_extract_identity_and_context():
    claims = Hn().extract(record("hn", "story:17418813"))  # Brex, domain from the post text
    company = next(c for c in claims if c.predicate == "name" and c.subject.kind == "company").subject
    assert company.identity == {"domain": "brex.com"}
    person = next(c for c in claims if c.predicate == "founded").subject
    assert set(person.identity) == {"hn_user"} and person.context is not None
    assert person.context[0].kind == "company" and person.context[0].identity == {"domain": "brex.com"}
    launch = next(c for c in claims if c.predicate == "posted").object
    assert launch.identity == {"hn_story": "17418813"}


def test_extract_without_domain_still_names_the_company():
    claims = Hn().extract(record("hn", "story:13886596"))  # Effective Altruism Funds: no link
    company = next(c for c in claims if c.predicate == "name" and c.subject.kind == "company")
    assert company.value == "Effective Altruism Funds" and company.subject.identity == {}
    assert company.subject.context and company.subject.context[0].identity.get("hn_user")
    # The launch name falls back to the company name when there is no tagline.
    assert any(
        c.predicate == "name" and c.subject.kind == "launch" and c.value == "Effective Altruism Funds"
        for c in claims
    )


def test_extract_unparsed_titles_emit_nothing():
    for ext in ("story:27563271", "story:24937661", "story:35613215", "story:30701792", "story:23861435"):
        assert Hn().extract(record("hn", ext)) == []


def test_extract_query_urls_are_not_launch_urls():
    claims = Hn().extract(record("hn", "story:26015145"))  # Balance: youtube.com/watch?v=...
    assert not [c for c in claims if c.predicate == "url"]
    assert not [c for c in claims if c.predicate == "domain"]
    claims = Hn().extract(record("hn", "story:13493587"))  # Voodoo: ?utm_source=hn is fine
    assert [c.value for c in claims if c.predicate == "url"] == ["https://voodoomfg.com/?utm_source=hn"]


def test_source_refs_are_stable_and_unique():
    for rec in records("hn"):
        a, b = Hn().extract(rec), Hn().extract(rec)
        assert [c.model_dump() for c in a] == [c.model_dump() for c in b]
        keys = [(c.source_ref, c.predicate) for c in a]
        assert len(keys) == len(set(keys)), rec.external_id
        assert all(c.source_ref.startswith("hn:") for c in a)


def test_same_author_facts_share_source_refs_across_stories():
    a = Hn().extract(record("hn", "story:38516795"))  # Slauth (YC S22)
    b = Hn().extract(record("hn", "story:34038663"))  # Slauth.io (YC S22), same author
    ra = {c.source_ref for c in a if c.source_ref.startswith("hn:user:")}
    rb = {c.source_ref for c in b if c.source_ref.startswith("hn:user:")}
    assert ra and ra == rb


def test_fixture_counts():
    recs = records("hn")
    assert 20 <= len(recs) <= 60
    parsed = [r for r in recs if parse_title(r.payload["title"])]
    assert len(recs) - len(parsed) >= 5  # unparsed shapes are represented


# ── fetch (mock transport) ─────────────────────────────────────────────────


def _algolia(stories: list[dict]):
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        calls.append(params)
        assert request.headers["User-Agent"].startswith("rebox-bot/")
        lo, hi = 0, 10**12
        for f in params.get("numericFilters", "").split(","):
            if f.startswith("created_at_i>="):
                lo = int(f.split(">=")[1])
            elif f.startswith("created_at_i<"):
                hi = int(f.split("<")[1])
        hits = sorted((s for s in stories if lo <= s["created_at_i"] < hi), key=lambda s: -s["created_at_i"])[
            : int(params["hitsPerPage"])
        ]
        return httpx.Response(200, json={"hits": hits, "nbHits": len(hits)})

    return handler, calls


def test_fetch_backfills_with_windows_and_sets_cursor(monkeypatch):
    import rebox_worker.connectors.hn as hn

    monkeypatch.setattr(hn, "PAGE_SIZE", 20)
    stories = [r.payload | {"_highlightResult": {"x": 1}} for r in records("hn")]
    handler, calls = _algolia(stories)
    conn = Hn(transport=httpx.MockTransport(handler), sleep=lambda s: None)
    items = list(conn.fetch(ctx(), None))
    recs = [i for i in items if isinstance(i, RawRecord)]
    assert len(recs) == len(stories) and len(calls) >= 3  # paginated by created_at_i windows
    assert len({r.external_id for r in recs}) == len(recs)
    assert all("_highlightResult" not in r.payload for r in recs)  # only kept fields stored
    assert [r.content_hash for r in recs if r.external_id == "story:36309120"] == [
        record("hn", "story:36309120").content_hash
    ]  # the same story fetched again hashes the same (skipped as unchanged)
    [cur] = [i for i in items if isinstance(i, CursorUpdate)]
    assert cur.cursor["created_at_i"] == max(s["created_at_i"] for s in stories)
    assert cur.cursor["last_run"]["unparsed_titles"] == sum(
        1 for s in stories if parse_title(s["title"]) is None
    )


def test_fetch_incremental_uses_lookback_and_limit_keeps_cursor():
    stories = [r.payload for r in records("hn")]
    newest = max(s["created_at_i"] for s in stories)
    handler, calls = _algolia(stories)
    conn = Hn(transport=httpx.MockTransport(handler), sleep=lambda s: None)
    items = list(conn.fetch(ctx(), {"created_at_i": newest}))
    assert calls[0]["numericFilters"] == f"created_at_i>={newest - 7 * 86400}"
    assert items and isinstance(items[-1], CursorUpdate)

    items = list(conn.fetch(ctx(limit=3), None))
    recs = [i for i in items if isinstance(i, RawRecord)]
    assert len(recs) == 3 and not [i for i in items if isinstance(i, CursorUpdate)]
    assert recs[0].payload["created_at_i"] == newest  # newest first


def test_fetch_retries_429_and_5xx():
    seq = [httpx.Response(429, headers={"Retry-After": "3"}), httpx.Response(503), None]
    slept: list[float] = []

    def handler(request):
        r = seq.pop(0)
        return r or httpx.Response(200, json={"hits": []})

    conn = Hn(transport=httpx.MockTransport(handler), sleep=slept.append, min_interval_s=1.0)
    assert [i for i in conn.fetch(ctx(), None) if isinstance(i, RawRecord)] == []
    assert slept == [1.0, 3.0, 1.0, 4.0, 1.0]  # interval, Retry-After, interval, backoff 2s*2^1, interval


def test_fetch_fails_fast_on_4xx():
    conn = Hn(transport=httpx.MockTransport(lambda r: httpx.Response(400)), sleep=lambda s: None)
    with pytest.raises(httpx.HTTPStatusError):
        list(conn.fetch(ctx(), None))


def test_raw_payload_is_json_roundtrippable():
    for rec in records("hn"):
        assert RawRecord.model_validate_json(rec.to_json()) == rec
        json.dumps(rec.payload)
