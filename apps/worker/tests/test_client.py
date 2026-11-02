import json

import httpx
import pytest

from rebox_worker.client import ClaimsClient, ValidationRejected, WriterError
from rebox_worker.models import ClaimWrite


def claims(n):
    return [
        ClaimWrite(
            subject={"mint": {"id": f"rbx:company:c{i}", "kind": "company"}},
            predicate="name",
            value=f"C{i}",
            source_ref=f"t:{i}",
        )
        for i in range(n)
    ]


W = "t-connector-000001"


def mock_client(handler, **kw):
    sleeps: list[float] = []
    c = ClaimsClient(
        "http://api.test", "tok", transport=httpx.MockTransport(handler), sleep=sleeps.append, **kw
    )
    return c, sleeps


def test_batches_of_500_with_auth():
    seen = []

    def handler(req: httpx.Request):
        body = __import__("json").loads(req.content)
        seen.append((req.url.path, req.headers["authorization"], body["layer"], len(body["claims"])))
        assert (body["worker"], body["run_id"]) == ("t-connector-000001", 7)
        return httpx.Response(200, json={"written": len(body["claims"]), "minted": 1})

    c, _ = mock_client(handler)
    res = c.write("public/test", claims(1201), worker="t-connector-000001", run_id=7)
    assert [s[3] for s in seen] == [500, 500, 201]
    assert seen[0][:3] == ("/internal/claims", "Bearer tok", "public/test")
    assert (res.written, res.minted) == (1201, 3)


def test_request_shape_omits_empty_optionals():
    c = claims(1)[0].to_request()
    assert c == {
        "subject": {"mint": {"id": "rbx:company:c0", "kind": "company"}},
        "predicate": "name",
        "source_ref": "t:0",
        "value": "C0",
    }


def test_retries_5xx_and_network_errors_with_backoff():
    calls = iter(["net", 503, 502, 200])

    def handler(req):
        r = next(calls)
        if r == "net":
            raise httpx.ConnectError("refused")
        return httpx.Response(r, json={"written": 1, "minted": 0} if r == 200 else {})

    c, sleeps = mock_client(handler, backoff_s=1.0)
    assert c.write("public/test", claims(1), worker=W).written == 1
    assert len(sleeps) == 3
    assert 0.8 <= sleeps[0] <= 1.2 and 1.6 <= sleeps[1] <= 2.4 and 3.2 <= sleeps[2] <= 4.8


def test_gives_up_after_max_retries():
    c, sleeps = mock_client(lambda req: httpx.Response(500), max_retries=2)
    with pytest.raises(WriterError, match="giving up after 3 attempts"):
        c.write("public/test", claims(1), worker=W)
    assert len(sleeps) == 2


def test_400_raises_with_details_and_no_retry():
    details = [{"index": 0, "message": "unknown predicate 'x'"}]
    c, sleeps = mock_client(lambda req: httpx.Response(400, json={"error": "validation", "details": details}))
    with pytest.raises(ValidationRejected) as e:
        c.write("public/test", claims(2), worker=W)
    assert e.value.details == details and "unknown predicate" in str(e.value)
    assert sleeps == []


def test_401_is_not_retried():
    c, sleeps = mock_client(lambda req: httpx.Response(401, json={"error": "unauthorized"}))
    with pytest.raises(WriterError, match="401"):
        c.write("public/test", claims(1), worker=W)
    assert sleeps == []


def test_against_fake_writer_round_trip(client, writer, owner, db):
    writer.fail_next = [503, "drop"]
    w = client.register_worker(name="t", kind="connector", source="t", layers=["public/test"])
    res = client.write("public/test", claims(3), worker=w)
    assert (res.written, res.minted) == (3, 3)
    assert (
        owner.execute("select count(*) n from entity where home_layer = 'public/test'").fetchone()["n"] == 3
    )
    # Fake writer enforces the contract: a non-normalized identity value is a 400.
    bad = ClaimWrite(
        subject={"id": "rbx:company:c0"}, predicate="domain", value="WWW.Acme.ai", source_ref="b"
    )
    with pytest.raises(ValidationRejected, match="not normalized"):
        client.write("public/test", [bad], worker=w)


# ── drop_rejected: one bad claim never sinks a batch (S12) ──


def _rejecting(bad: set[str], seen: list[list[str]], extra=None):
    """A writer that 400s (per index) every claim whose source_ref is in ``bad``."""

    def handler(req: httpx.Request):
        body = json.loads(req.content)
        refs = [c["source_ref"] for c in body["claims"]]
        seen.append(refs)
        details = [{"index": i, "message": f"{r} is bad"} for i, r in enumerate(refs) if r in bad]
        if extra:
            details += extra(body["claims"])
        if details:
            return httpx.Response(400, json={"error": "validation", "details": details})
        return httpx.Response(200, json={"written": len(refs), "minted": 0})

    return handler


def test_drop_rejected_resends_the_rest():
    seen: list[list[str]] = []
    c, sleeps = mock_client(_rejecting({"t:1", "t:3"}, seen))
    res = c.write("public/test", claims(5), worker=W, drop_rejected=True)
    assert seen == [["t:0", "t:1", "t:2", "t:3", "t:4"], ["t:0", "t:2", "t:4"]]
    assert res.written == 3 and sleeps == []
    assert [(r.claim.source_ref, r.reason) for r in res.rejected] == [
        ("t:1", "t:1 is bad"),
        ("t:3", "t:3 is bad"),
    ]


def test_drop_rejected_all_rejected_continues():
    seen: list[list[str]] = []
    c, _ = mock_client(_rejecting({"t:0", "t:1"}, seen))
    res = c.write("public/test", claims(2), worker=W, drop_rejected=True)
    assert res.written == 0 and len(res.rejected) == 2 and len(seen) == 1


def test_drop_rejected_handles_a_second_round_and_joins_reasons():
    """Reference checks run after shape checks, so a re-send may be refused for new claims."""
    seen: list[list[str]] = []

    def extra(cs):
        if len(seen) == 1:  # first round: two messages for claim 0
            return [{"index": 0, "message": "also wrong"}]
        if len(seen) == 2:  # second round: the (new) first claim fails a reference check
            return [{"index": 0, "message": "subject does not exist"}]
        return []

    c, _ = mock_client(_rejecting({"t:0"}, seen, extra))
    res = c.write("public/test", claims(3), worker=W, drop_rejected=True)
    assert seen == [["t:0", "t:1", "t:2"], ["t:1", "t:2"], ["t:2"]]
    assert res.written == 1
    assert [(r.claim.source_ref, r.reason) for r in res.rejected] == [
        ("t:0", "t:0 is bad; also wrong"),
        ("t:1", "subject does not exist"),
    ]


def _claim(ref: str, subject: dict, obj: dict | None = None) -> ClaimWrite:
    if obj is None:
        return ClaimWrite(subject=subject, predicate="name", value=ref, source_ref=ref)
    return ClaimWrite(subject=subject, predicate="worked_at", object=obj, source_ref=ref)


def test_drop_rejected_drops_claims_that_depend_on_a_rejected_mint():
    E = {"mint": {"id": "rbx:company:e", "kind": "company"}}
    F = {"mint": {"id": "rbx:company:f", "kind": "company"}}
    P = {"mint": {"id": "rbx:person:p", "kind": "person"}}
    batch = [
        _claim("mints-e", E),  # refused: e is never minted
        _claim("needs-e", {"id": "rbx:company:p2"}, {"id": "rbx:company:e"}),  # dependent
        _claim("needs-e-mints-p", P, {"id": "rbx:company:e"}),  # dependent (and p's only minter)
        _claim("needs-p", {"id": "rbx:person:p"}),  # dependent of a dependent
        _claim("mints-f", F),  # refused, but f is also minted by the next claim
        _claim("also-mints-f", F),
        _claim("needs-f", {"id": "rbx:company:f"}),  # kept: f still gets minted
    ]
    seen: list[list[str]] = []
    c, _ = mock_client(_rejecting({"mints-e", "mints-f"}, seen))
    res = c.write("public/test", batch, worker=W, drop_rejected=True)
    assert seen[-1] == ["also-mints-f", "needs-f"] and res.written == 2
    assert {r.claim.source_ref: r.reason for r in res.rejected} == {
        "mints-e": "mints-e is bad",
        "mints-f": "mints-f is bad",
        "needs-e": "depends on rejected mint rbx:company:e",
        "needs-e-mints-p": "depends on rejected mint rbx:company:e",
        "needs-p": "depends on rejected mint rbx:person:p",
    }


def test_drop_rejected_dependents_across_batches():
    E = {"mint": {"id": "rbx:company:e", "kind": "company"}}
    G = {"mint": {"id": "rbx:company:g", "kind": "company"}}
    batch = [
        _claim("mints-e", E),
        _claim("mints-g", G),
        _claim("needs-e", {"id": "rbx:company:e"}),  # next batch: never sent
        _claim("needs-g", {"id": "rbx:company:g"}),  # next batch: g was written in batch 1
    ]
    seen: list[list[str]] = []
    c, _ = mock_client(_rejecting({"mints-e"}, seen))
    res = c.write("public/test", batch, worker=W, drop_rejected=True, batch_size=2)
    assert seen == [["mints-e", "mints-g"], ["mints-g"], ["needs-g"]]
    assert res.written == 2 and [r.claim.source_ref for r in res.rejected] == ["mints-e", "needs-e"]


@pytest.mark.parametrize(
    "details",
    [
        [{"index": -1, "message": "worker t-connector-000001 is suspended, not active"}],
        [{"index": 0, "message": "bad"}, {"index": -1, "message": "layer not allowed"}],
        [{"index": 7, "message": "index out of range"}],
        [],
    ],
)
def test_drop_rejected_still_raises_on_batch_level_400(details):
    c, _ = mock_client(lambda req: httpx.Response(400, json={"error": "validation", "details": details}))
    with pytest.raises(ValidationRejected):
        c.write("public/test", claims(2), worker=W, drop_rejected=True)


def test_drop_rejected_still_raises_on_auth_and_exhausted_retries():
    c, _ = mock_client(lambda req: httpx.Response(403, json={"error": "forbidden"}))
    with pytest.raises(WriterError, match="403"):
        c.write("public/test", claims(1), worker=W, drop_rejected=True)
    c, sleeps = mock_client(lambda req: httpx.Response(503), max_retries=1)
    with pytest.raises(WriterError, match="giving up"):
        c.write("public/test", claims(1), worker=W, drop_rejected=True)
    assert len(sleeps) == 1


def test_drop_rejected_against_fake_writer(client, writer, owner, db):
    w = client.register_worker(name="t", kind="connector", source="t", layers=["public/test"])
    bad = ClaimWrite(
        subject={"mint": {"id": "rbx:company:bad", "kind": "company"}},
        predicate="domain",
        value="WWW.Acme.ai",
        source_ref="bad",
    )
    batch = claims(3)
    batch.insert(1, bad)
    res = client.write("public/test", batch, worker=w, drop_rejected=True)
    assert (res.written, res.minted) == (3, 3)
    assert [(r.claim.source_ref, r.reason) for r in res.rejected] == [
        ("bad", "identity value for domain not normalized")
    ]
    ids = [r["id"] for r in owner.execute("select id from entity order by id")]
    assert ids == ["rbx:company:c0", "rbx:company:c1", "rbx:company:c2"]
