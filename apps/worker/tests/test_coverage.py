"""Coverage checker (S14): match paths, stats, report, enqueue, read-only guarantees."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pytest

from rebox_worker import cli, coverage
from rebox_worker.coverage import InputRow, check, enqueue, fold_name, parse_rows, run
from rebox_worker.predicates import load_predicates

REPO = Path(__file__).resolve().parents[3]
SAMPLE = REPO / "fixtures" / "coverage" / "sample.csv"
EXPLORER = "http://explorer.test"


# Three more facts on every seeded entity, so only `thin=True` ones are thin (<= 3 facts).
EXTRA = (("description", "seeded"), ("x_followers", "100"), ("avatar", "https://img.example/a.png"))


def seed(owner, eid, kind, *facts, layer="public/manual", created_at=None, merged_into=None, thin=False):
    """Entity + facts, inserted as the owner (test setup only). A fact is (predicate, value) or
    (predicate, None, object_id[, valid_from])."""
    facts = facts if thin else (*facts, *EXTRA)
    owner.execute(
        "insert into entity (id, kind, home_layer, created_at, merged_into) "
        "values (%s, %s, %s, coalesce(%s::timestamptz, now()), %s)",
        (eid, kind, layer, created_at, merged_into),
    )
    for pred, val, *rest in facts:
        obj = rest[0] if rest else None
        valid_from = rest[1] if len(rest) > 1 else None
        owner.execute(
            "insert into claim (layer_id, subject, predicate, value, object_id, valid_from, source_ref) "
            "values (%s, %s, %s, to_jsonb(%s::text), %s, %s, %s)",
            (layer, eid, pred, val, obj, valid_from, f"seed:{eid}:{pred}:{val}:{obj}"),
        )


@pytest.fixture
def graph(db, owner):
    """The graph the sample CSV is checked against."""
    owner.execute("delete from search_name")  # truncate (conftest) does not fire its trigger
    owner.execute("delete from frontier")
    c, p = "company", "person"
    seed(owner, "rbx:company:ditto", c, ("name", "Ditto"), ("domain", "ditto.ai"), ("x_handle", "dittoai"))
    seed(
        owner,
        "rbx:company:stripe",
        c,
        ("name", "Stripe"),
        ("domain", "stripe.com"),
        ("wikidata", "Q7624104"),
        ("founded_year", "2010"),
        layer="public/wikidata",
    )
    seed(
        owner,
        "rbx:company:stripex",
        c,
        ("name", "Stripe"),
        layer="public/x",
        merged_into="rbx:company:stripe",
    )
    seed(owner, "rbx:company:acme", c, ("name", "Acme"))
    seed(
        owner,
        "rbx:company:acmeold",
        c,
        ("name", "Acme Old"),
        ("domain", "acme-old.example"),
        merged_into="rbx:company:acme",
    )
    seed(owner, "rbx:company:figma1", c, ("name", "Figma"), ("domain", "figma.com"), created_at="2024-01-01")
    seed(
        owner,
        "rbx:company:figma2",
        c,
        ("name", "Figma Inc"),
        ("domain", "figma.com"),
        created_at="2025-01-01",
    )
    seed(owner, "rbx:company:vercel", c, ("name", "Vercel"))
    seed(owner, "rbx:company:northwind", c, ("name", "Northwind, Inc."))
    seed(owner, "rbx:company:fabrikam", c, ("name", "Fabrikam"))
    seed(owner, "rbx:company:mercury1", c, ("name", "Mercury"))
    seed(owner, "rbx:company:mercury2", c, ("name", "Mercury"))
    seed(owner, "rbx:person:ada", p, ("name", "Ada Quill"), ("x_handle", "adaquill"), layer="public/x")
    seed(owner, "rbx:person:bram", p, ("name", "Bram Okonkwo"), ("x_handle", "bramo"), layer="public/x")
    seed(owner, "rbx:person:cleo", p, ("name", "Cleo Vance"), ("github", "cleovance"), layer="public/github")
    seed(owner, "rbx:person:dov", p, ("name", "Dov Marchetti"))
    seed(owner, "rbx:person:eliza", p, ("name", "Eliza Thornbury"))
    seed(owner, "rbx:person:zoe", p, ("name", "Zoë Lindgren"))
    seed(owner, "rbx:person:jon1", p, ("name", "Jon Rivera"))
    seed(owner, "rbx:person:jon2", p, ("name", "Jon Rivera"))
    seed(owner, "rbx:person:mira1", p, ("name", "Mira Castell"), ("worked_at", None, "rbx:company:ditto"))
    seed(
        owner,
        "rbx:person:mira2",
        p,
        ("name", "Mira Castell"),
        ("founded", None, "rbx:company:stripe", "2010-01-01"),
    )
    seed(
        owner,
        "rbx:person:pip",
        p,
        ("name", "Pip Harlow"),
        ("x_handle", "pipharlow"),
        layer="public/x",
        thin=True,
    )
    seed(owner, "rbx:person:theo", p, ("name", "Theo Brandt"), ("x_handle", "theo_other"), layer="public/x")
    return db


def rows_of(db, text: str) -> list[InputRow]:
    reader = csv.DictReader(io.StringIO(text))
    with db.view() as conn:
        preds = load_predicates(conn)
    return parse_rows(reader.fieldnames or [], list(reader), preds)


def one(db, text: str) -> coverage.RowResult:
    [res] = check(db, rows_of(db, text)).results
    return res


def counts(owner) -> dict[str, int]:
    tables = ("claim", "entity", "change", "resolution_log", "review_item", "merge_log", "search_name")
    return {t: owner.execute(f"select count(*) as n from {t}").fetchone()["n"] for t in tables}


# ── each match path ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("csv_text", "status", "entity"),
    [
        ("name,x\nAda,https://x.com/AdaQuill\n", "matched", "rbx:person:ada"),  # X URL form
        ("x_handle\n@BramO\n", "matched", "rbx:person:bram"),  # @Handle, case
        ("twitter\nhttps://twitter.com/bramo?lang=en\n", "matched", "rbx:person:bram"),
        ("kind,website\ncompany,https://www.ditto.ai/\n", "matched", "rbx:company:ditto"),
        ("domain\nwww.Ditto.ai\n", "matched", "rbx:company:ditto"),
        ("github\nhttps://github.com/CleoVance/\n", "matched", "rbx:person:cleo"),
        ("kind,wikidata\ncompany,q7624104\n", "matched", "rbx:company:stripe"),
        ("name\nDov Marchetti\n", "matched_name", "rbx:person:dov"),  # exact folded name
        ("name\n  dov   MARCHETTI \n", "matched_name", "rbx:person:dov"),
        ("name\nZoe Lindgren\n", "matched_name", "rbx:person:zoe"),  # accents folded
        ("name\nElisa Thornbury\n", "matched_name", "rbx:person:eliza"),  # trigram
        ("kind,name\ncompany,Northwind\n", "matched_name", "rbx:company:northwind"),  # legal suffix
        ("kind,name\ncompany,Contoso (Fabrikam)\n", "matched_name", "rbx:company:fabrikam"),  # alias
        ("name\nJon Rivera\n", "ambiguous", None),
        ("name,x\nNobody Known,ghost_handle_404\n", "missing", None),
        ("name\nWren Calloway\n", "missing", None),
    ],
)
def test_match_paths(graph, csv_text, status, entity):
    res = one(graph, csv_text)
    assert (res.match.status, res.match.entity_id) == (status, entity)


def test_hard_key_beats_name_and_respects_kind(graph):
    # The x handle wins over a name that matches someone else.
    assert one(graph, "name,x\nDov Marchetti,adaquill\n").match.entity_id == "rbx:person:ada"
    # A company's domain is never a person's hard key; a person row does not match a company.
    res = one(graph, "kind,name,x\nperson,Ditto Bot,dittoai\n")
    assert res.match.status == "missing"


def test_hard_key_follows_merges_and_lists_duplicates(graph):
    res = one(graph, "kind,domain\ncompany,acme-old.example\n")
    assert (res.match.status, res.match.entity_id) == ("matched", "rbx:company:acme")
    res = one(graph, "kind,website\ncompany,figma.com\n")
    assert res.match.entity_id == "rbx:company:figma1"  # the oldest, as the resolver picks
    assert [c for c, _ in res.match.candidates] == ["rbx:company:figma1", "rbx:company:figma2"]


def test_ambiguous_lists_candidates(graph):
    res = one(graph, "kind,name\ncompany,Mercury\n")
    assert res.match.status == "ambiguous"
    assert sorted(c for c, _ in res.match.candidates) == ["rbx:company:mercury1", "rbx:company:mercury2"]


def test_person_with_company_disambiguates(graph):
    text = (
        'name,company\nMira Castell,Ditto\nMira Castell,"Stripe, Inc."\nMira Castell,Initech\nMira Castell,\n'
    )
    res = check(graph, rows_of(graph, text)).results
    assert [(r.match.status, r.match.entity_id) for r in res] == [
        ("matched_name", "rbx:person:mira1"),
        ("matched_name", "rbx:person:mira2"),
        ("ambiguous", None),  # no candidate has an edge to Initech: still two namesakes
        ("ambiguous", None),
    ]
    assert res[0].match.how == "name+company"


def test_name_candidate_with_a_different_handle_is_a_namesake(graph):
    assert one(graph, "name,x\nTheo Brandt,theob_real\n").match.status == "missing"
    assert one(graph, "name\nTheo Brandt\n").match.entity_id == "rbx:person:theo"  # no handle: fine


def test_stats(graph):
    res = one(graph, "kind,domain\ncompany,stripe.com\n")
    s = res.stats
    assert s is not None
    assert s.name == "Stripe"
    # name (2 layers, same value) + domain + wikidata + founded_year + 3 extras (also on the
    # merged-in entity, same values).
    assert s.facts == 7
    assert s.edges == 1  # mira2 founded it
    assert s.sources == ["public/manual", "public/wikidata", "public/x"]
    assert s.timeline is True
    pip = one(graph, "x\npipharlow\n")
    assert pip.thin and pip.stats is not None and pip.stats.facts == 2 and pip.stats.timeline is False


# ── the fixture CSV, report and summary ───────────────────────────────────


def test_sample_csv_report(graph, tmp_path, owner):
    before = counts(owner)
    out = tmp_path / "report.csv"
    buf = io.StringIO()
    summary = run(graph, SAMPLE, out=out, as_json=False, do_enqueue=False, explorer_url=EXPLORER, stream=buf)
    assert counts(owner) == before  # read-only: no claim, change, entity, log or review rows

    with SAMPLE.open() as f:
        input_fields = csv.DictReader(f).fieldnames or []
    with out.open() as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        assert reader.fieldnames == [*input_fields, *coverage.OUTPUT_COLUMNS]
    assert len(rows) == 30
    wrong = [(r["name"], r["company"], r["expect"], r["status"]) for r in rows if r["status"] != r["expect"]]
    assert wrong == []
    assert {r["list_owner"] for r in rows} == {"deal team", "events"}  # unknown columns pass through
    assert rows[15]["linkedin"] == "https://www.linkedin.com/in/ada-quill-0"  # echoed, not matched

    ditto = rows[0]
    assert ditto["entity_id"] == "rbx:company:ditto" and ditto["rebox_name"] == "Ditto"
    assert ditto["url"] == f"{EXPLORER}/e/rbx%3Acompany%3Aditto"
    assert ditto["facts"] == "6" and ditto["timeline"] == "n" and ditto["sources"] == "public/manual"
    assert "rbx:company:mercury1 (Mercury, 1.00)" in rows[9]["candidates"]

    st = summary["status"]
    assert summary["rows"] == 30 and summary["by_kind"] == {"person": 15, "company": 15}
    assert st["all"]["matched"]["n"] == 10 and st["all"]["missing"]["n"] == 8
    assert st["company"]["missing"]["pct"] == 33.3
    assert [t["label"] for t in summary["thin"]] == ["Pip Harlow"]
    text = buf.getvalue()
    assert "matched_name" in text and "thin matches (<= 3 facts): 1: Pip Harlow" in text
    assert (
        "missing (8): Linear, Retool, Rippling, Deel, Gusto, Theo Brandt, Nobody Known, Wren Calloway" in text
    )
    assert "top sources in matches: " in text


def test_report_column_collision(graph, tmp_path):
    src = tmp_path / "list.csv"
    src.write_text("Kind,Name,Status\ncompany,Vercel,prospect\n")
    out = tmp_path / "out.csv"
    run(graph, src, out=out, as_json=False, do_enqueue=False, explorer_url=EXPLORER, stream=io.StringIO())
    [row] = list(csv.DictReader(out.open()))
    assert row["Status"] == "prospect" and row["coverage_status"] == "matched_name"


# ── enqueue ──────────────────────────────────────────────────────────────


def frontier_rows(owner):
    return owner.execute(
        "select source, key, kind, label, score, depth, discovered_by, status"
        " from frontier order by source, key"
    ).fetchall()


def test_enqueue_upserts_and_is_idempotent(graph, owner):
    before = counts(owner)
    cov = check(graph, rows_of(graph, SAMPLE.read_text()))
    assert enqueue(graph, cov.results, "sample") == {"x_spider": 3, "exa": 6}
    first = frontier_rows(owner)
    assert sorted((r["source"], r["key"]) for r in first) == [
        ("exa", "Deel"),
        ("exa", "Gusto"),  # no domain from an x.com URL: the name is the key
        ("exa", "Wren Calloway"),
        ("exa", "linear.app"),
        ("exa", "retool.com"),
        ("exa", "rippling.com"),
        ("x_spider", "@ghost_handle_404"),
        ("x_spider", "@pipharlow"),  # thin match
        ("x_spider", "@theob_real"),
    ]
    assert all(r["status"] == "new" and r["score"] == 1.0 and r["discovered_by"] == ["coverage:sample"]
               for r in first)  # fmt: skip
    pip = next(r for r in first if r["key"] == "@pipharlow")
    assert (pip["kind"], pip["label"], pip["depth"]) == ("person", "pipharlow", 0)

    assert enqueue(graph, cov.results, "sample") == {"x_spider": 3, "exa": 6}
    assert frontier_rows(owner) == first  # idempotent

    owner.execute("update frontier set status = 'fetched' where key = '@pipharlow'")
    enqueue(graph, cov.results, "other-list")
    after = {r["key"]: r for r in frontier_rows(owner)}
    assert after["@pipharlow"]["discovered_by"] == ["coverage:other-list", "coverage:sample"]
    assert after["@pipharlow"]["status"] == "fetched"  # never moved backwards
    assert counts(owner) == before  # frontier only: no graph writes


def test_cli_json_and_enqueue(graph, owner, app_url, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", app_url)
    monkeypatch.setenv("EXPLORER_URL", EXPLORER)
    src = tmp_path / "mylist.csv"
    src.write_text(SAMPLE.read_text())
    assert cli.main(["coverage", str(src), "--json", "--enqueue"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["summary"]["queued"] == {"x_spider": 3, "exa": 6}
    assert data["summary"]["report"] == str(tmp_path / "mylist.report.csv")
    assert (tmp_path / "mylist.report.csv").is_file()
    assert data["rows"][0]["url"] == f"{EXPLORER}/e/rbx%3Acompany%3Aditto"
    keys = {r["discovered_by"][0] for r in frontier_rows(owner)}
    assert keys == {"coverage:mylist"}


# ── parsing (no database needed beyond the vocabulary) ───────────────────


def test_parse_kind_guess_and_columns(graph):
    rows = rows_of(
        graph,
        "Full_Name,First_Name,Last_Name,Website,Company,LinkedIn,Type\n"
        "Acme,,,https://acme.example,,,\n"  # website, nothing person-ish -> company
        ",Ann,Lee,https://annlee.dev,,,\n"  # first/last name -> person
        "Bo,,,,Acme,,\n"  # company column -> person
        "Cy,,,https://cy.dev,,https://linkedin.com/in/cy,\n"  # linkedin /in/ -> person
        "Dee Corp,,,,,,company\n"  # explicit kind
        "Ed,,,https://x.com/ed,,,org\n",  # platform URL is never a domain key
    )
    assert [(r.kind, r.kind_guessed, r.name) for r in rows] == [
        ("company", True, "Acme"),
        ("person", True, "Ann Lee"),
        ("person", True, "Bo"),
        ("person", True, "Cy"),
        ("company", False, "Dee Corp"),
        ("company", False, "Ed"),
    ]
    assert ("domain", "acme.example") in rows[0].pairs
    assert all(p != "domain" for p, _ in rows[1].pairs)  # domain applies to companies only
    assert ("website", "https://annlee.dev") in rows[1].pairs
    assert rows[5].domain is None and ("website", "https://x.com/ed") in rows[5].pairs


def test_invalid_values_are_ignored(graph):
    [r] = rows_of(graph, "kind,domain,wikidata\ncompany,localhost,not-a-qid\n")
    assert r.pairs == [] and len(r.invalid) == 2


def test_fold_name():
    assert fold_name("Anysphere, Inc.", "company") == "anysphere"
    assert fold_name("Inc", "company") == "inc"
    assert fold_name("Jo Co", "person") == "jo co"
