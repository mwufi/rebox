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
