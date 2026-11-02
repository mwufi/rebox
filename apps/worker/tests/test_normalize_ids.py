import base64
import hashlib

import pytest

from rebox_worker.ids import IDENTITY_KEY_ORDER, mint_id, strongest_key
from rebox_worker.normalize import NORMALIZERS, get_normalizer, normalize_name

# (normalizer, raw, expected) — the CONTRACTS §4 examples first, then edge cases.
CASES = [
    ("qid", "q42", "Q42"),
    ("qid", " Q7624104 ", "Q7624104"),
    ("qid", "https://www.wikidata.org/wiki/Q62", "Q62"),
    ("qid", "http://www.wikidata.org/entity/q95", "Q95"),
    ("handle", "https://x.com/Jack", "jack"),
    ("handle", "@Jack", "jack"),
    ("handle", "jack", "jack"),
    ("handle", "https://twitter.com/@Jack?lang=en", "jack"),
    ("handle", "github.com/OctoCat/", "octocat"),
    ("handle", "https://github.com/octocat/hello-world", "octocat"),
    ("domain", "https://www.Stripe.com/about", "stripe.com"),
    ("domain", "Stripe.com", "stripe.com"),
    ("domain", "www.acme.ai:8443/x?y=1", "acme.ai"),
    ("domain", "http://user@Sub.Example.co.uk.", "sub.example.co.uk"),
    ("url", "https://www.acme.ai/", "https://acme.ai"),
    ("url", "HTTPS://WWW.Acme.AI/Launch/?ref=hn#top", "https://acme.ai/Launch"),
    ("url", "acme.ai/blog/", "https://acme.ai/blog"),
    ("url", "http://acme.ai:8080/", "http://acme.ai:8080"),
    ("email", "  Ada@Example.COM ", "ada@example.com"),
    ("exact", "  41234567 ", "41234567"),
    ("exact", 41234567, "41234567"),
]


@pytest.mark.parametrize(("name", "raw", "expected"), CASES)
def test_normalizers(name, raw, expected):
    norm = NORMALIZERS[name]
    assert norm(raw) == expected
    assert norm(expected) == expected  # idempotent


@pytest.mark.parametrize(
    ("name", "raw"),
    [
        ("qid", "P31"),
        ("qid", "42"),
        ("domain", "localhost"),
        ("email", "nope"),
        ("exact", "  "),
        ("handle", "@"),
        ("url", ""),
        ("domain", None),
    ],
)
def test_normalizers_reject(name, raw):
    with pytest.raises(ValueError):
        NORMALIZERS[name](raw)


def test_unknown_normalizer():
    assert get_normalizer(None) is NORMALIZERS["exact"]
    with pytest.raises(ValueError):
        get_normalizer("soundex")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Maya Chen", "maya chen"),
        ("  MAYA   chen ", "maya chen"),
        ("José Álvarez", "jose alvarez"),
        ("Acme, Inc.", "acme inc"),
        ("O'Brien-Smith", "obriensmith"),
        ("Zoë\tKrav", "zoe krav"),
    ],
)
def test_normalize_name(raw, expected):
    assert normalize_name(raw) == expected


def _expected(material: str) -> str:
    return base64.b32encode(hashlib.sha256(material.encode()).digest()).decode().lower().rstrip("=")[:20]


def test_mint_id_formula():
    assert mint_id("company", {"domain": "stripe.com"}, "hn", "x") == "rbx:company:" + _expected(
        "company|domain|stripe.com"
    )
    assert mint_id("person", {}, "hn", "hn:story:1:posted") == "rbx:person:" + _expected(
        "person|src|hn|hn:story:1:posted"
    )


def test_mint_id_is_deterministic_and_uses_strongest_key():
    a = mint_id("company", {"website": "https://acme.ai", "domain": "acme.ai", "wikidata": "Q1"}, "s", "r")
    b = mint_id("company", {"wikidata": "Q1"}, "other-source", "other-ref")
    assert a == b  # wikidata outranks domain and website; source/ref irrelevant with a key
    assert a.startswith("rbx:company:") and len(a.split(":")[2]) == 20
    assert mint_id("person", {"wikidata": "Q1"}, "s", "r") != a  # kind is part of the hash


def test_mint_id_multi_valued_is_order_independent():
    x = mint_id("company", {"domain": ["b.com", "a.com"]}, "s", "r")
    y = mint_id("company", {"domain": ["a.com", "b.com"]}, "s", "r")
    assert x == y == mint_id("company", {"domain": "a.com"}, "s", "r")


def test_strongest_key_order():
    assert IDENTITY_KEY_ORDER[0] == "wikidata" and IDENTITY_KEY_ORDER[-1] == "email"
    assert strongest_key({"email": "a@b.c", "github": "x"}) == ("github", "x")
    assert strongest_key({"name": "x"}) is None
