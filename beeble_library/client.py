"""BeebleClient -- the only module in this library that imports httpx.

That is deliberate: it is the seam for MockTransport tests and the single home of retry/backoff
and the rate-limit token buckets.

The buckets are **module-level singletons**, not client instance state. Two hero nodes in one graph
each run their own poll loop and must share one budget; per-instance buckets would let a graph with
N waiters issue N times the allowed rate.
"""

from __future__ import annotations

import asyncio
import time
from types import TracebackType
from typing import Any, Final, Self

import httpx

from beeble_library.constants import (
    API_BASE_URL,
    API_KEY_HEADER,
    DEFAULT_CONCURRENCY,
    DEFAULT_READ_RPM,
    DEFAULT_WRITE_RPM,
    LIST_LIMIT_MAX,
    LIST_LIMIT_MIN,
)
from beeble_library.errors import BeebleError, RetryPolicy, from_response

DEFAULT_TIMEOUT_SECONDS: Final = 60.0
MAX_RETRY_ATTEMPTS: Final = 5
BACKOFF_BASE_SECONDS: Final = 2.0
BACKOFF_CAP_SECONDS: Final = 60.0
SLOT_WAIT_SECONDS: Final = 15.0
"""CONCURRENT_LIMIT_EXCEEDED is not a backoff problem -- a slot frees when a job finishes, not when
we wait longer. Poll at a steady interval instead of escalating."""


class _TokenBucket:
    """Async token bucket, refilling at ``rpm`` tokens per minute.

    One bucket per direction (read / write), shared process-wide. The lock serialises waiters,
    which is what we want: it makes the wait fair and keeps the token maths correct.

    Note: the lock binds to the first event loop that uses it. The engine runs a single loop, so
    this is fine in practice; driving one bucket from two loops would raise.
    """

    def __init__(self, rpm: int) -> None:
        self._rpm = max(1, rpm)
        self._tokens = float(self._rpm)
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    @property
    def rpm(self) -> int:
        return self._rpm

    def resize(self, rpm: int | None) -> None:
        """Resize from a live account limit.

        ``None`` means *account default / unlimited* in Beeble's schema -- explicitly NOT zero --
        so it leaves the current size alone rather than throttling to a standstill.
        """
        if rpm is None:
            return
        new_rpm = max(1, int(rpm))
        if new_rpm == self._rpm:
            return
        self._rpm = new_rpm
        self._tokens = min(self._tokens, float(new_rpm))

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(
                    float(self._rpm),
                    self._tokens + (now - self._updated) * (self._rpm / 60.0),
                )
                self._updated = now

                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return

                await asyncio.sleep((1.0 - self._tokens) / (self._rpm / 60.0))


# Process-wide. See the module docstring for why these are not instance attributes.
_READ_BUCKET = _TokenBucket(DEFAULT_READ_RPM)
_WRITE_BUCKET = _TokenBucket(DEFAULT_WRITE_RPM)
_CONCURRENCY_LIMIT = DEFAULT_CONCURRENCY


def read_bucket() -> _TokenBucket:
    return _READ_BUCKET


def write_bucket() -> _TokenBucket:
    return _WRITE_BUCKET


def concurrency_limit() -> int:
    return _CONCURRENCY_LIMIT


def reset_buckets(read_rpm: int = DEFAULT_READ_RPM, write_rpm: int = DEFAULT_WRITE_RPM) -> None:
    """Reset the process-wide buckets. For tests -- production code should call sync_rate_limits()."""
    global _READ_BUCKET, _WRITE_BUCKET, _CONCURRENCY_LIMIT  # noqa: PLW0603
    _READ_BUCKET = _TokenBucket(read_rpm)
    _WRITE_BUCKET = _TokenBucket(write_rpm)
    _CONCURRENCY_LIMIT = DEFAULT_CONCURRENCY


def _limit_value(node: Any) -> int | None:
    """Pull ``.limit`` out of a RateLimitInfo-shaped dict, treating null as 'leave alone'."""
    if isinstance(node, dict):
        value = node.get("limit")
        if isinstance(value, int):
            return value
    return None


class BeebleClient:
    """Async client for the six SwitchX operations that an API key can reach."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = API_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={API_KEY_HEADER: api_key},
            timeout=timeout,
            transport=transport,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- plumbing ---------------------------------------------------------------------------

    async def _request(self, method: str, url: str, *, write: bool, **kwargs: Any) -> dict[str, Any]:
        """Issue a rate-limited, retrying request and return the parsed body.

        Raises:
            BeebleError: On any non-200 response, after retries are exhausted.
        """
        bucket = _WRITE_BUCKET if write else _READ_BUCKET
        last_error: BeebleError | None = None

        for attempt in range(MAX_RETRY_ATTEMPTS):
            await bucket.acquire()
            response = await self._client.request(method, url, **kwargs)

            if response.status_code == httpx.codes.OK:
                return self._parse_json(response)

            error = self._to_error(response)
            last_error = error

            if not error.is_retryable or attempt == MAX_RETRY_ATTEMPTS - 1:
                raise error

            await asyncio.sleep(self._retry_delay(error, attempt, response))

        # Unreachable: the loop either returns or raises. Kept for type-checker completeness.
        raise last_error if last_error else BeebleError(None, "Request failed with no response")

    @staticmethod
    def _parse_json(response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError as e:
            msg = f"Expected JSON from {response.request.url}, got {response.text[:200]!r}"
            raise BeebleError(None, msg, http_status=response.status_code) from e
        if not isinstance(body, dict):
            msg = f"Expected a JSON object from {response.request.url}, got {type(body).__name__}"
            raise BeebleError(None, msg, http_status=response.status_code)
        return body

    @staticmethod
    def _to_error(response: httpx.Response) -> BeebleError:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        return from_response(payload, response.status_code)

    @staticmethod
    def _retry_delay(error: BeebleError, attempt: int, response: httpx.Response) -> float:
        """Backoff for RATE_LIMIT_EXCEEDED, steady polling for CONCURRENT_LIMIT_EXCEEDED."""
        if error.retry == RetryPolicy.AWAIT_SLOT:
            return SLOT_WAIT_SECONDS

        retry_after = response.headers.get("retry-after")
        if retry_after:
            try:
                return min(float(retry_after), BACKOFF_CAP_SECONDS)
            except ValueError:
                pass

        return min(BACKOFF_BASE_SECONDS**attempt, BACKOFF_CAP_SECONDS)

    # -- account ----------------------------------------------------------------------------

    async def account_info(self) -> dict[str, Any]:
        """GET /account/info -- spending limit and live rate limits."""
        return await self._request("GET", "/account/info", write=False)

    async def account_billing(self) -> dict[str, Any]:
        """GET /account/billing -- balance, period usage, per-meter totals. No unit prices.

        Itself capped at 5 RPM. Fetch once per batch and cache; never once per shot.
        """
        return await self._request("GET", "/account/billing", write=False)

    async def sync_rate_limits(self) -> dict[str, Any]:
        """Read live limits and resize the process-wide buckets.

        Call once before a batch. A ``limit`` of null means default/unlimited and leaves the bucket
        untouched -- it does not mean zero.
        """
        global _CONCURRENCY_LIMIT  # noqa: PLW0603

        info = await self.account_info()
        limits = info.get("rate_limits")
        if isinstance(limits, dict):
            rpm = _limit_value(limits.get("rpm"))
            _READ_BUCKET.resize(rpm)
            _WRITE_BUCKET.resize(rpm)

            concurrency = _limit_value(limits.get("concurrency"))
            if concurrency is not None:
                _CONCURRENCY_LIMIT = max(1, concurrency)

        return info

    # -- uploads ----------------------------------------------------------------------------

    async def create_upload(self, filename: str) -> dict[str, Any]:
        """POST /uploads -> {id, upload_url, beeble_uri}. The presigned PUT expires in 1 hour."""
        return await self._request("POST", "/uploads", write=True, json={"filename": filename})

    async def put_upload(self, upload_url: str, data: bytes, content_type: str | None = None) -> None:
        """PUT bytes to a presigned URL.

        Deliberately outside the token buckets: this goes to object storage, not to the Beeble API,
        so it does not consume the account's request budget.

        Raises:
            BeebleError: If the upload is rejected.
        """
        headers = {"Content-Type": content_type} if content_type else {}
        response = await self._client.put(upload_url, content=data, headers=headers)
        if response.status_code not in (httpx.codes.OK, httpx.codes.CREATED, httpx.codes.NO_CONTENT):
            msg = f"Upload PUT failed: {response.text[:200]}"
            raise BeebleError(None, msg, http_status=response.status_code)

    # -- generations ------------------------------------------------------------------------

    async def submit_generation(self, body: dict[str, Any]) -> dict[str, Any]:
        """POST /switchx/generations -> {id: "swx_...", status, seed, ...}. Returns immediately."""
        return await self._request("POST", "/switchx/generations", write=True, json=body)

    async def get_job(self, job_id: str) -> dict[str, Any]:
        """GET /switchx/generations/{job_id}. Mints FRESH signed output URLs on every call."""
        return await self._request("GET", f"/switchx/generations/{job_id}", write=False)

    async def list_jobs(self, limit: int = 20, page_token: str | None = None) -> dict[str, Any]:
        """GET /switchx/generations. Ordering is undocumented -- do not assume newest-first."""
        params: dict[str, Any] = {"limit": max(LIST_LIMIT_MIN, min(LIST_LIMIT_MAX, limit))}
        if page_token:
            params["page_token"] = page_token
        return await self._request("GET", "/switchx/generations", write=False, params=params)

    async def download(self, url: str) -> bytes:
        """Fetch a signed output URL.

        Outside the token buckets and sent without the API key -- signed URLs carry their own auth
        and are served from storage, not the API.

        Raises:
            BeebleError: If the download fails.
        """
        response = await self._client.get(url, headers={API_KEY_HEADER: ""})
        if response.status_code != httpx.codes.OK:
            msg = f"Download failed for {url[:120]}: {response.text[:200]}"
            raise BeebleError(None, msg, http_status=response.status_code)
        return response.content


def output_urls(job: dict[str, Any]) -> dict[str, str | None]:
    """Pull the signed URLs out of a completed job.

    They live under ``output`` (a SwitchXOutputUrls object), not at the top level, and every field
    is nullable. Confirmed against the OpenAPI schema.
    """
    output = job.get("output")
    if not isinstance(output, dict):
        return {"render": None, "source": None, "alpha": None}
    return {key: output.get(key) if isinstance(output.get(key), str) else None for key in ("render", "source", "alpha")}


def job_error(job: dict[str, Any]) -> str:
    """Read the failure reason off a job.

    ``error`` is nullable-but-*present*, so ``job.get("error", "unknown")`` never fires its default.
    """
    return job.get("error") or "unknown"
