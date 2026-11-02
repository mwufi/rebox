"""Recorded live fixtures (fixtures/raw/x_spider, fixtures/raw/exa), trimmed: real public X
accounts from @davidbooth's and @yoni_michael's following (fields we use only) and Exa results.
Fixtures were recorded on 2026-11-02; extraction must stay stable on them."""

from __future__ import annotations

from connectors.conftest import records  # noqa: I001

from rebox_worker.connectors.exa import Exa
from rebox_worker.connectors.x_spider import XSpider
from rebox_worker.pipeline import run_replay
from rebox_worker.rawstore import FileRawStore

from .conftest import FIXTURES


def by_handle(recs):
    return {r.payload["user"]["username"].lower(): r for r in recs if r.payload["type"] == "x_user"}


def facts(claims):
    return {c.predicate: c.value for c in claims}


def test_fixture_sizes_and_shapes():
    xs, ex = records("x_spider"), records("exa")
    users = [r for r in xs if r.payload["type"] == "x_user"]
    assert len(users) <= 30 and len(ex) <= 20
    assert {r.payload["type"] for r in xs} == {"x_user", "x_following", "x_post"}
    for r in users:  # only the fields we use
        assert set(r.payload["user"]) <= {
            "id", "username", "name", "description", "location", "url", "entities", "public_metrics",
            "profile_image_url", "verified_type", "created_at",
        }  # fmt: skip
    for r in ex:
        assert r.payload["type"] == "exa_result" and r.payload["url"].startswith("http")


def test_recorded_people_and_companies():
    sp = XSpider(seeds={})
    u = by_handle(records("x_spider"))
    kar = sp.extract(u["karpathy"])
    assert kar[0].subject.kind == "person"
    assert facts(kar)["name"] == "Andrej Karpathy" and facts(kar)["x_id"] == "33836629"
    assert facts(kar)["avatar"].endswith("_400x400.jpg")
    db = facts(sp.extract(u["davidbooth"]))
    assert db["location"].startswith("San Francisco") and db["website"] == "http://a16z.com"
    assert "domain" not in db  # a person's website is never a company domain
    tecton = sp.extract(u["tectonai"])
    assert tecton[0].subject.kind == "company" and facts(tecton)["domain"] == "tecton.ai"
    assert "location" not in facts(tecton) and "description" in facts(tecton)
    assert sp.extract(u["idf"])[0].subject.kind == "company"  # verified_type government
    assert facts(sp.extract(u["typedef_ai"]))["domain"] == "typedef.ai"  # utm query ignored
    for r in u.values():
        for c in sp.extract(r):
            if c.predicate == "website":
                assert "t.co/" not in c.value


def test_recorded_following_pages():
    sp = XSpider(seeds={})
    pages = [r for r in records("x_spider") if r.payload["type"] == "x_following"]
    assert {p.payload["seed"]["username"].lower() for p in pages} == {"davidbooth", "yoni_michael"}
    assert {p.payload["seed_set"] for p in pages} == {"dbooth", "yoni"}
    for p in pages:
        cs = sp.extract(p)
        assert len(cs) == len(p.payload["users"]) and all(c.predicate == "follows" for c in cs)
        assert all(c.subject.name is None and c.object.name is None for c in cs)


def test_recorded_posts_make_no_claims():
    sp = XSpider(seeds={})
    posts = [r for r in records("x_spider") if r.payload["type"] == "x_post"]
    assert posts and all(sp.extract(r) == [] for r in posts)
    assert all({"id", "text", "created_at"} <= set(r.payload["post"]) for r in posts)


def test_recorded_exa_homepages_and_articles():
    ex = Exa(queries=[])
    names = {}
    for r in records("exa"):
        cs = ex.extract(r)
        if not r.payload["homepage"]:
            assert cs == []
            continue
        f = facts(cs)
        names[f["domain"]] = f["name"]
    assert names["ditto.ai"] == "Ditto" and names["known.com"] == "Known" and names["kebra.com"] == "Kebra"
    assert len(names) == 10


def test_recorded_replay_end_to_end(deps, owner, tmp_path):
    """Replay the recorded fixtures through resolver + writer: no rejects, one name per entity,
    karpathy/booth/yoni are people, follows edges exist for both seeds."""
    store = FileRawStore(tmp_path / "rec", deps.db)
    for src in ("x_spider", "exa"):
        for r in records(src):
            store.put(r)
    deps.raw = store
    s1 = run_replay(XSpider(seeds={}), deps)
    s2 = run_replay(Exa(queries=[]), deps)
    assert s1.rejected == 0 and s2.rejected == 0 and s1.extract_errors == 0
    assert (
        owner.execute(
            "select subject from claim where predicate='name' group by 1 having count(*)>1"
        ).fetchall()
        == []
    )
    n = owner.execute(
        """select s.value_text h, count(*) n from claim f
             join claim s on s.subject = f.subject and s.predicate = 'x_handle'
            where f.predicate = 'follows' group by 1 order by 1"""
    ).fetchall()
    assert [(r["h"], r["n"]) for r in n] == [("davidbooth", 13), ("yoni_michael", 13)]
    kinds = {r["kind"]: r["n"] for r in owner.execute("select kind, count(*) n from entity group by 1")}
    assert kinds["company"] >= 10 + 7 and kinds["person"] >= 15
    assert FIXTURES.joinpath("x_spider").is_dir()
