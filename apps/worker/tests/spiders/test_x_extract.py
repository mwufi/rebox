"""x_spider extract (pure): classification, profile facts, follows edges, payload shapes."""

from __future__ import annotations

import pytest

from rebox_worker.connectors.x_spider import (
    XSpider,
    avatar_url,
    bio_text,
    classify,
    expansion_eligible,
    following_claims,
    own_site_domain,
    user_claims,
    user_record,
)
from rebox_worker.models import RawRecord

from .conftest import user


def facts(claims) -> dict[str, object]:
    return {c.predicate: c.value for c in claims}


# ── person vs company ────────────────────────────────────────────────────

CLASSIFY_CASES = [
    # (description, user, expected kind)
    ("verified business", user("1", "acmehq", name="Acme", verified_type="business"), "company"),
    ("verified government", user("2", "sfgov", name="SF.gov", verified_type="government"), "company"),
    ("official account", user("3", "zed", name="Zed", bio="The official account of Zed."), "company"),
    (
        "website = handle, product voice",
        user(
            "4",
            "linear",
            name="Linear",
            bio="Linear is a purpose-built tool for planning",
            url="https://linear.app",
        ),
        "company",
    ),
    (
        "company-like name + website",
        user("5", "nao_labs", name="Nao Labs", bio="Data IDE.", url="https://getnao.io"),
        "company",
    ),
    (
        "company-like name + we voice",
        user("6", "foo", name="Foo AI", bio="We build agents for SMBs"),
        "company",
    ),
    (
        "founder bio, own site",
        user(
            "7",
            "jdoe",
            name="Jane Doe",
            bio="co-founder & CEO @acme. ex-Stripe. she/her",
            url="https://janedoe.com",
        ),
        "person",
    ),
    ("plain person", user("8", "bob", name="Bob Smith", bio="I make things"), "person"),
    ("empty bio defaults to person", user("9", "x9", name="x9"), "person"),
    (
        "person with company in name tag",
        user("10", "sam", name="Sam Lee 🌉", bio="building @acme (YC W24), prev Google"),
        "person",
    ),
    (
        "fund",
        user("11", "somecap", name="Some Capital", bio="Early stage fund. We back founders."),
        "company",
    ),
    (
        "handle = name + ai",
        user("13", "TectonAI", name="Tecton", bio="Build and deploy production-grade ML applications"),
        "company",
    ),
    (
        "handle = name + _ai",
        user("14", "typedef_ai", name="typedef", bio="AI agents for data engineering"),
        "company",
    ),
    (
        "a ... lab, join us",
        user("15", "fabric", name="Fabric", bio="a social computing lab. join us"),
        "company",
    ),
    (
        "one weak signal is not enough",
        user("17", "fabric", name="Fabric", bio="a social computing lab"),
        "person",
    ),
    ("person handle+name", user("16", "jdoeai", name="J Doe", bio="I build AI. engineer"), "person"),
    (
        "website is link-in-bio, not company",
        user("12", "kay", name="Kay", bio="writer", url="https://linktr.ee/kay"),
        "person",
    ),
]


@pytest.mark.parametrize(("desc", "u", "kind"), CLASSIFY_CASES, ids=[c[0] for c in CLASSIFY_CASES])
def test_classify(desc, u, kind):
    assert classify(u) == kind


def test_person_facts_with_expanded_website_location_avatar():
    u = user(
        "100",
        "Jane_Doe",
        name="Jane Doe 🌉",
        bio="cofounder @acme · prev https://t.co/abc",
        location="San Francisco, CA",
        url="https://janedoe.com/",
        followers=1234,
    )
    u["entities"]["description"] = {
        "urls": [{"url": "https://t.co/abc", "expanded_url": "https://stripe.com"}]
    }
    cs = user_claims(u)
    f = facts(cs)
    assert f == {
        "x_id": "100",
        "x_handle": "jane_doe",
        "name": "Jane Doe",
        "headline": "cofounder @acme · prev https://stripe.com",
        "location": "San Francisco, CA",
        "website": "https://janedoe.com/",
        "avatar": "https://pbs.twimg.com/profile_images/100/a_400x400.jpg",
        "x_followers": 1234,
    }
    subj = cs[0].subject
    assert subj.kind == "person"
    assert subj.identity == {"x_id": "100", "x_handle": "jane_doe"}
    assert {c.source_ref for c in cs} == {f"x_spider:user:100:{p}" for p in f}
    assert all(c.object is None for c in cs)


def test_company_facts_description_domain_no_location():
    u = user(
        "200",
        "linear",
        name="Linear",
        bio="Linear is a purpose-built tool for planning and building products",
        location="San Francisco",
        url="https://linear.app",
        verified_type="business",
    )
    cs = user_claims(u)
    f = facts(cs)
    assert f["description"].startswith("Linear is")
    assert "headline" not in f and "location" not in f
    assert f["website"] == "https://linear.app"
    assert f["domain"] == "linear.app"
    assert cs[0].subject.kind == "company"
    assert cs[0].subject.identity == {"x_id": "200", "x_handle": "linear", "domain": "linear.app"}


def test_company_with_foreign_website_gets_no_domain():
    """A business account linking to someone else's site (a launch page, a parent) gets no domain."""
    u = user(
        "201",
        "acmehq",
        name="Acme",
        verified_type="business",
        url="https://www.ycombinator.com/companies/acme",
    )
    f = facts(user_claims(u))
    assert "domain" not in f and "website" not in f


def test_website_never_tco():
    u = user("300", "p", name="P Q")
    u["url"] = "https://t.co/zzz"
    u["entities"] = {"url": {"urls": [{"url": "https://t.co/zzz", "expanded_url": "https://t.co/other"}]}}
    assert "website" not in facts(user_claims(u))


@pytest.mark.parametrize(
    ("src", "want"),
    [
        (
            "https://pbs.twimg.com/profile_images/1/abc_normal.jpg",
            "https://pbs.twimg.com/profile_images/1/abc_400x400.jpg",
        ),
        (
            "https://pbs.twimg.com/profile_images/1/abc_normal.png",
            "https://pbs.twimg.com/profile_images/1/abc_400x400.png",
        ),
        (
            "https://pbs.twimg.com/profile_images/1/abc_bigger",
            "https://pbs.twimg.com/profile_images/1/abc_400x400",
        ),
        ("https://abs.twimg.com/sticky/default_profile_images/default_profile_normal.png", None),
        ("http://insecure/x_normal.jpg", None),
        (None, None),
    ],
)
def test_avatar_sizing(src, want):
    assert avatar_url(src) == want


def test_own_site_domain_variants():
    assert own_site_domain(user("1", "acme", name="Acme", url="https://www.acme.com/")) == "acme.com"
    assert own_site_domain(user("1", "acmehq", name="Acme", url="https://acme.com")) == "acme.com"
    assert own_site_domain(user("1", "x", name="Nao Labs", url="https://getnao.io")) == "getnao.io"
    assert own_site_domain(user("1", "acme", name="Acme", url="https://app.acme.ai/login")) == "acme.ai"
    assert own_site_domain(user("1", "acme", name="Acme", url="https://other.com")) is None
    assert own_site_domain(user("1", "acme", name="Acme", url="https://github.com/acme")) is None


def test_bio_text_expands_tco():
    u = user("1", "a", bio="see https://t.co/q and more")
    u["entities"] = {"description": {"urls": [{"url": "https://t.co/q", "expanded_url": "https://q.dev/x"}]}}
    assert bio_text(u) == "see https://q.dev/x and more"


# ── follows edges ────────────────────────────────────────────────────────


def test_follows_edges_seed_to_people_and_companies():
    seed = user("1", "DavidBooth", name="David Booth", bio="investor", location="SF")
    followed = [
        user("2", "jane", name="Jane Doe", bio="founder"),
        user("3", "acme", name="Acme", verified_type="business", url="https://acme.com"),
    ]
    cs = following_claims(seed, followed)
    assert [
        (c.subject.identity["x_id"], c.predicate, c.object.kind, c.object.identity["x_id"]) for c in cs
    ] == [
        ("1", "follows", "person", "2"),
        ("1", "follows", "company", "3"),
    ]
    assert cs[0].subject.kind == "person" and cs[0].subject.identity["x_handle"] == "davidbooth"
    assert cs[1].object.identity["domain"] == "acme.com"
    assert [c.source_ref for c in cs] == ["x_spider:follows:1:2", "x_spider:follows:1:3"]
    assert all(c.value is None for c in cs)


def test_extract_dispatches_on_record_type():
    sp = XSpider(seeds={})
    seed = user("1", "s", name="S T")
    page = RawRecord.new(
        "x_spider",
        "following:1:0",
        {"type": "x_following", "seed": seed, "seed_set": "dbooth", "users": [user("2", "a")]},
    )
    post = RawRecord.new(
        "x_spider", "post:9", {"type": "x_post", "post": {"id": "9", "text": "hi"}, "author": seed}
    )
    assert [c.predicate for c in sp.extract(page)] == ["follows"]
    assert sp.extract(post) == []
    assert {c.predicate for c in sp.extract(user_record(seed))} >= {"x_id", "x_handle", "name"}
    assert XSpider.worker_for(page) == "dbooth"


def test_extract_is_deterministic():
    sp = XSpider(seeds={})
    rec = user_record(user("5", "z", name="Zed Labs", bio="We build", url="https://zed.dev"))
    assert sp.extract(rec) == sp.extract(rec)


# ── expansion eligibility ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("u", "score", "ok"),
    [
        (user("1", "a", name="A B", bio="founder", location="San Francisco, CA"), 1, True),
        (user("1", "a", name="A B", bio="building in SF 🌉", location="Earth"), 1, True),  # bio counts
        (user("1", "a", name="A B", bio="building in SF", location=""), 1, True),
        (user("1", "a", name="A B", bio="founder", location="Bay Area"), 1, True),
        (user("1", "a", name="A B", bio="founder", location="New York"), 3, False),
        (user("1", "a", name="A B", bio="founder"), 2, True),  # no location, 2 seeds
        (user("1", "a", name="A B", bio="founder"), 1, False),
        (
            user("1", "acme", name="Acme", verified_type="business", location="SF"),
            3,
            False,
        ),  # companies never
    ],
)
def test_expansion_eligible(u, score, ok):
    assert expansion_eligible(u, score) is ok
