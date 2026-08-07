"""Client tests driven entirely by httpx MockTransport -- no live calls in the suite."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from beeble_library import client as client_mod
from beeble_library.client import BeebleClient, job_error, output_urls, reset_buckets
from beeble_library.constants import API_KEY_HEADER
from beeble_library.errors import BeebleError


@pytest.fixture(autouse=True)
def _fast_buckets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the rate limiter out of the way, and never really sleep during retry tests."""
    reset_buckets(read_rpm=10_000, write_rpm=10_000)

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(client_mod.asyncio, "sleep", _no_sleep)


def make_client(handler: Any) -> BeebleClient:
    return BeebleClient("test-key", transport=httpx.MockTransport(handler))


def error_body(code: str, message: str = "boom") -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}


# -- happy paths ------------------------------------------------------------------------------


async def test_submit_generation_posts_body_and_returns_job() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        seen["api_key"] = request.headers.get(API_KEY_HEADER)
        return httpx.Response(200, json={"id": "swx_1", "status": "in_queue", "seed": 42})

    async with make_client(handler) as client:
        job = await client.submit_generation({"generation_type": "video"})

    assert job["id"] == "swx_1"
    assert seen["method"] == "POST"
    assert seen["url"].endswith("/switchx/generations")
    assert seen["api_key"] == "test-key"


async def test_get_job_hits_the_by_id_endpoint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/switchx/generations/swx_9")
        return httpx.Response(200, json={"id": "swx_9", "status": "completed"})

    async with make_client(handler) as client:
        job = await client.get_job("swx_9")

    assert job["status"] == "completed"


@pytest.mark.parametrize(("requested", "expected"), [(0, 1), (1, 1), (20, 20), (100, 100), (500, 100)])
async def test_list_jobs_clamps_limit_to_the_documented_range(requested: int, expected: int) -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["limit"] = request.url.params.get("limit")
        return httpx.Response(200, json={"jobs": []})

    async with make_client(handler) as client:
        await client.list_jobs(limit=requested)

    assert seen["limit"] == str(expected)


async def test_download_omits_the_api_key_for_signed_urls() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["api_key"] = request.headers.get(API_KEY_HEADER)
        return httpx.Response(200, content=b"video-bytes")

    async with make_client(handler) as client:
        data = await client.download("https://storage.example/signed/render.mp4")

    assert data == b"video-bytes"
    assert not seen["api_key"], "signed URLs carry their own auth; sending the key is wrong"


async def test_put_upload_accepts_the_success_statuses() -> None:
    for status in (200, 201, 204):

        def handler(request: httpx.Request, _status: int = status) -> httpx.Response:
            assert request.method == "PUT"
            return httpx.Response(_status)

        async with make_client(handler) as client:
            await client.put_upload("https://storage.example/put", b"bytes", "video/mp4")


async def test_put_upload_raises_on_rejection() -> None:
    async with make_client(lambda _r: httpx.Response(403, text="denied")) as client:
        with pytest.raises(BeebleError) as excinfo:
            await client.put_upload("https://storage.example/put", b"bytes")

    assert excinfo.value.http_status == 403


# -- error mapping ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (400, "SOURCE_TOO_LARGE"),
        (401, "INVALID_API_KEY"),
        (402, "INSUFFICIENT_BALANCE"),
        (404, "JOB_NOT_FOUND"),
    ],
)
async def test_non_retryable_errors_raise_immediately(status: int, code: str) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, json=error_body(code))

    async with make_client(handler) as client:
        with pytest.raises(BeebleError) as excinfo:
            await client.get_job("swx_1")

    assert excinfo.value.code == code
    assert calls == 1, "a terminal error must not be retried"


async def test_rate_limit_is_retried_then_succeeds() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(429, json=error_body("RATE_LIMIT_EXCEEDED"))
        return httpx.Response(200, json={"id": "swx_1", "status": "completed"})

    async with make_client(handler) as client:
        job = await client.get_job("swx_1")

    assert job["status"] == "completed"
    assert calls == 3


async def test_retries_are_bounded() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, json=error_body("RATE_LIMIT_EXCEEDED"))

    async with make_client(handler) as client:
        with pytest.raises(BeebleError):
            await client.get_job("swx_1")

    assert calls == client_mod.MAX_RETRY_ATTEMPTS


async def test_credit_deduction_failed_is_retried_despite_being_402() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(402, json=error_body("CREDIT_DEDUCTION_FAILED"))
        return httpx.Response(200, json={"id": "swx_1"})

    async with make_client(handler) as client:
        assert (await client.submit_generation({}))["id"] == "swx_1"

    assert calls == 2


async def test_concurrent_limit_uses_a_steady_wait_not_escalating_backoff() -> None:
    delays: list[float] = []

    async def _record(seconds: float) -> None:
        delays.append(seconds)

    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 4:
            return httpx.Response(429, json=error_body("CONCURRENT_LIMIT_EXCEEDED"))
        return httpx.Response(200, json={"id": "swx_1"})

    original = client_mod.asyncio.sleep
    client_mod.asyncio.sleep = _record  # type: ignore[assignment]
    try:
        async with make_client(handler) as client:
            await client.submit_generation({})
    finally:
        client_mod.asyncio.sleep = original  # type: ignore[assignment]

    assert delays == [client_mod.SLOT_WAIT_SECONDS] * 3, "waiting longer does not free a slot"


async def test_retry_after_header_is_honoured() -> None:
    delays: list[float] = []

    async def _record(seconds: float) -> None:
        delays.append(seconds)

    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, json=error_body("RATE_LIMIT_EXCEEDED"), headers={"retry-after": "7"})
        return httpx.Response(200, json={})

    original = client_mod.asyncio.sleep
    client_mod.asyncio.sleep = _record  # type: ignore[assignment]
    try:
        async with make_client(handler) as client:
            await client.get_job("swx_1")
    finally:
        client_mod.asyncio.sleep = original  # type: ignore[assignment]

    assert delays == [7.0]


async def test_non_json_body_becomes_a_beeble_error() -> None:
    async with make_client(lambda _r: httpx.Response(200, text="<html>nope</html>")) as client:
        with pytest.raises(BeebleError):
            await client.get_job("swx_1")


# -- rate limit syncing -----------------------------------------------------------------------


async def test_sync_rate_limits_resizes_buckets_from_live_values() -> None:
    payload = {"rate_limits": {"rpm": {"limit": 600}, "concurrency": {"limit": 50}}}

    async with make_client(lambda _r: httpx.Response(200, json=payload)) as client:
        await client.sync_rate_limits()

    assert client_mod.read_bucket().rpm == 600
    assert client_mod.write_bucket().rpm == 600
    assert client_mod.concurrency_limit() == 50


async def test_null_limit_means_default_not_zero() -> None:
    reset_buckets(read_rpm=5, write_rpm=5)
    payload = {"rate_limits": {"rpm": {"limit": None}, "concurrency": {"limit": None}}}

    async with make_client(lambda _r: httpx.Response(200, json=payload)) as client:
        await client.sync_rate_limits()

    assert client_mod.read_bucket().rpm == 5, "null means default/unlimited, never zero"


async def test_missing_rate_limits_block_is_tolerated() -> None:
    reset_buckets(read_rpm=5, write_rpm=5)

    async with make_client(lambda _r: httpx.Response(200, json={"email": "a@b.c"})) as client:
        await client.sync_rate_limits()

    assert client_mod.read_bucket().rpm == 5


# -- response helpers -------------------------------------------------------------------------


def test_output_urls_reads_the_nested_output_object() -> None:
    job = {
        "id": "swx_1",
        "output": {"render": "https://x/r.mp4", "source": "https://x/s.mp4", "alpha": None},
    }
    assert output_urls(job) == {"render": "https://x/r.mp4", "source": "https://x/s.mp4", "alpha": None}


@pytest.mark.parametrize("job", [{}, {"output": None}, {"output": "not-a-dict"}])
def test_output_urls_tolerates_a_missing_output(job: dict[str, Any]) -> None:
    assert output_urls(job) == {"render": None, "source": None, "alpha": None}


def test_job_error_handles_nullable_but_present_error() -> None:
    # `error` is present-and-null on success, so .get("error", default) never fires its default.
    assert job_error({"status": "failed", "error": None}) == "unknown"
    assert job_error({"status": "failed", "error": "GPU exploded"}) == "GPU exploded"
    assert job_error({}) == "unknown"


# -- the rate limiter itself ------------------------------------------------------------------


async def test_token_bucket_is_process_wide_not_per_instance() -> None:
    reset_buckets(read_rpm=5, write_rpm=5)
    handler = lambda _r: httpx.Response(200, json={})  # noqa: E731

    async with make_client(handler) as a, make_client(handler) as b:
        assert client_mod.read_bucket() is client_mod.read_bucket()
        # Two clients, one budget: both must consume from the same bucket object.
        before = client_mod.read_bucket()
        await a.get_job("swx_1")
        await b.get_job("swx_2")
        assert client_mod.read_bucket() is before


async def test_bucket_resize_never_drops_below_one() -> None:
    bucket = client_mod._TokenBucket(5)
    bucket.resize(0)
    assert bucket.rpm == 1
