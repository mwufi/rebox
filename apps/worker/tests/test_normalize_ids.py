import base64
import hashlib
import json
from pathlib import Path

import pytest

from rebox_worker.ids import IDENTITY_KEY_ORDER, mint_id, strongest_key
from rebox_worker.normalize import NORMALIZERS, get_normalizer, normalize_name

# CONTRACTS §4: the cases both services run (apps/api/test/normalize.test.ts loads the same file).
SHARED = json.loads((Path(__file__).resolve().parents[3] / "contracts" / "normalize-cases.json").read_text())
SHARED_CASES = SHARED["cases"]


def _case_id(c):
    return f"{c['normalizer']}:{c['input']!r}"


def test_shared_cases_cover_every_normalizer():
    assert len(SHARED_CASES) >= 60
    assert {c["normalizer"] for c in SHARED_CASES} == set(NORMALIZERS)


@pytest.mark.parametrize("case", [c for c in SHARED_CASES if not c.get("error")], ids=_case_id)
def test_shared_normalizer_cases(case):
    norm = NORMALIZERS[case["normalizer"]]
    assert norm(case["input"]) == case["output"]
    assert norm(case["output"]) == case["output"]  # a fixed point: what the writer checks


@pytest.mark.parametrize("case", [c for c in SHARED_CASES if c.get("error")], ids=_case_id)
def test_shared_normalizer_errors(case):
    with pytest.raises(ValueError):
        NORMALIZERS[case["normalizer"]](case["input"])


def test_normalizers_are_idempotent_on_random_inputs():
    """normalize(normalize(x)) == normalize(x), else the writer rejects what the worker sends."""
    import random

    rnd = random.Random(12)
    alphabet = list("wW.:/@?#[]-_ aZ09") + ["www.", "http://", "https://", "x.com/", "\u00a0", "\u00dc", "%"]
    for _ in range(20000):
        raw = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(1, 12)))
        for name, norm in NORMALIZERS.items():
            try:
                once = norm(raw)
            except ValueError:
                continue
            assert norm(once) == once, (name, raw, once)


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
