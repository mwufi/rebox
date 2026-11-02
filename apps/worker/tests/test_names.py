"""fold_name: the folded-name key of the resolver's company name pass (S16)."""

import pytest

from rebox_worker.names import (
    LEGAL_SUFFIXES,
    converges_by_name,
    domain_label,
    fold_name,
    is_placeholder_company,
    is_safe_name_target,
)


@pytest.mark.parametrize(
    ("kind", "raw", "expected"),
    [
        # every kind: NFKC, casefold, accents, trim, whitespace
        ("person", "  Maya   CHEN ", "maya chen"),
        ("person", "José Álvarez", "jose alvarez"),
        ("person", "Zoë\tKrav", "zoe krav"),
        ("person", "Straße", "strasse"),  # casefold, not lower
        ("person", "ＡＤＡ", "ada"),  # NFKC: full-width letters
        ("person", "Ada Inc.", "ada inc."),  # company rules do not apply to persons
        ("person", "The Rock (Dwayne)", "the rock (dwayne)"),
        ("place", "San Francisco, CA", "san francisco, ca"),
        # companies: legal suffixes
        ("company", "Google LLC", "google"),
        ("company", "Stripe, Inc.", "stripe"),
        ("company", "Stripe Inc", "stripe"),
        ("company", "Notion Labs, Inc.", "notion labs"),
        ("company", "Foo L.L.C.", "foo"),
        ("company", "Foo Ltd", "foo"),
        ("company", "Tesla Motors Corp.", "tesla motors"),
        ("company", "Acme Corporation", "acme"),
        ("company", "Coca-Cola Co", "coca-cola"),
        ("company", "Acme Co., Ltd.", "acme"),  # repeatedly
        ("company", "Société Générale S.A.", "societe generale"),
        ("company", "Acme SA", "acme"),
        ("company", "  ＡＣＭＥ   GmbH ", "acme"),
        ("company", "Barclays PLC", "barclays"),
        ("company", "Anthropic, PBC", "anthropic"),
        # only whole trailing words, never inside a word
        ("company", "Costco", "costco"),
        ("company", "Incorporate", "incorporate"),
        ("company", "Pinc", "pinc"),
        # one trailing parenthetical
        ("company", "Ramp (YC W19)", "ramp"),
        ("company", "Acme (YC W21) (fka Foo)", "acme (yc w21)"),
        ("company", "Acme, Inc. (YC W21)", "acme"),
        ("company", "Acme (formerly X) Inc", "acme (formerly x)"),  # closing brackets are kept
        ("company", "(stealth)", "(stealth)"),  # nothing left otherwise
        # leading "the"
        ("company", "The Browser Company", "browser company"),
        ("company", "the co", "co"),
        ("company", "Theranos", "theranos"),
        # trailing punctuation
        ("company", "Yahoo!", "yahoo"),
        ("company", "Acme...", "acme"),
        ("company", "Y-Combinator", "y-combinator"),  # inner punctuation is kept
        # never folds to nothing
        ("company", "The", "the"),
        ("company", "Inc.", "inc"),
        ("company", "Co", "co"),
        ("company", "!!!", "!!!"),
        ("company", "", ""),
        ("company", None, ""),
    ],
)
def test_fold_name(kind, raw, expected):
    assert fold_name(kind, raw) == expected


@pytest.mark.parametrize("suffix", LEGAL_SUFFIXES)
def test_every_legal_suffix_is_stripped(suffix):
    assert fold_name("company", f"Acme {suffix}") == "acme"
    assert fold_name("company", f"Acme, {suffix.upper()}") == "acme"


def test_fold_is_idempotent():
    for raw in ("Acme Co., Ltd.", "The Browser Company", "Ramp (YC W19)", "Société Générale S.A."):
        once = fold_name("company", raw)
        assert fold_name("company", once) == once


@pytest.mark.parametrize("raw", ["Stealth", "Stealth Startup", "stealth mode", "Self-Employed", "Freelance"])
def test_placeholder_names(raw):
    assert is_placeholder_company(fold_name("company", raw))


def test_real_names_are_not_placeholders():
    assert not is_placeholder_company(fold_name("company", "Stealth Rocket Inc."))
    assert not is_placeholder_company(fold_name("company", "Google"))


# ── safe-target guard helpers ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("domain", "label"),
    [
        ("google.com", "google"),
        ("notion.so", "notion"),
        ("labs.ramp.com", "ramp"),
        ("bbc.co.uk", "bbc"),
        ("news.bbc.co.uk", "bbc"),
        ("atlassian.com.au", "atlassian"),
        ("coca-cola.com", "coca-cola"),
        ("co.uk", None),
        ("localhost", None),
        ("ramp.com.", "ramp"),
    ],
)
def test_domain_label(domain, label):
    assert domain_label(domain) == label


@pytest.mark.parametrize(
    ("folded", "facts", "safe"),
    [
        ("capital", {}, False),  # keyed by something else only
        ("capital", {"has_wikidata": True}, True),  # (a)
        ("google", {"domains": ["google.com"]}, True),  # (b)
        ("notion", {"domains": ["notion.so", "makenotion.com"]}, True),
        ("ramp", {"domains": ["labs.ramp.com"]}, True),
        ("coca-cola", {"domains": ["coca-cola.com"]}, True),  # punctuation ignored on both sides
        ("browser company", {"domains": ["browsercompany.com"]}, True),  # spaces ignored
        ("monzo bank", {"domains": ["monzo.co.uk"]}, False),
        ("base", {"domains": ["base.org.example"]}, False),  # label "org", not "base"
        ("balance", {"incoming_subjects": 5}, True),  # (c)
        ("balance", {"incoming_subjects": 4}, False),
    ],
)
def test_is_safe_name_target(folded, facts, safe):
    assert (
        is_safe_name_target(
            folded,
            has_wikidata=facts.get("has_wikidata", False),
            domains=facts.get("domains", ()),
            incoming_subjects=facts.get("incoming_subjects", 0),
        )
        is safe
    )


@pytest.mark.parametrize(
    ("folded", "converges"),
    [
        ("base", False),
        ("ramp", False),
        ("ey", False),
        ("notion", True),
        ("capital", True),
        ("base camp", True),
        ("a b", True),
    ],
)
def test_converges_by_name(folded, converges):
    assert converges_by_name(folded) is converges
