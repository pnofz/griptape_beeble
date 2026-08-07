"""Turn whatever a node hands us into a URI Beeble can actually fetch.

The problem this exists for: an artifact coming out of another Griptape node points at
``http://localhost:8124/...``. Beeble's servers cannot reach that, and the failure surfaces as
``SOURCE_UNREACHABLE``. Every URI-producing path has to download the bytes locally and either PUT
them to a presigned upload URL (preferred) or inline them as base64.

This module never imports httpx. Network work goes through the BeebleClient passed in, which keeps
client.py the single seam for mock-transport tests.
"""

from __future__ import annotations

import base64
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlparse

from beeble_library.constants import (
    BASE64_INFLATION,
    DATA_URI_MAX_BYTES,
    FILENAME_MAX_CHARS,
    FILENAME_MIN_CHARS,
    UPLOAD_EXTENSIONS,
)

if TYPE_CHECKING:
    from beeble_library.client import BeebleClient

LOCAL_HOSTS: Final[frozenset[str]] = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"})  # noqa: S104

STRATEGY_UPLOAD: Final = "upload"
STRATEGY_BASE64: Final = "base64"
STRATEGY_PASSTHROUGH: Final = "passthrough"
STRATEGIES: Final[tuple[str, ...]] = (STRATEGY_UPLOAD, STRATEGY_BASE64, STRATEGY_PASSTHROUGH)

SCHEME_BEEBLE: Final = "beeble"
SCHEME_DATA: Final = "data"
SCHEME_HTTPS: Final = "https"
SCHEME_LOCAL_URL: Final = "local_url"
SCHEME_PATH: Final = "path"


@dataclass(frozen=True)
class ResolvedURI:
    """The outcome of resolving a source to something Beeble can fetch."""

    uri: str
    scheme: str
    """One of the SCHEME_* constants -- what the *input* was, before resolution."""
    size_bytes: int | None = None
    uploaded: bool = False


class URIError(ValueError):
    """A source could not be turned into a usable URI."""


def extract_location(source: Any) -> str:
    """Pull a location string out of an artifact, dict, or path.

    Serialized artifacts arrive as dicts, so this has to handle both the object and the wire form.

    Raises:
        URIError: If no location can be found.
    """
    if isinstance(source, str):
        return source
    if isinstance(source, Path):
        return str(source)

    if isinstance(source, dict):
        for key in ("value", "url", "location", "path", "uri"):
            candidate = source.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
        msg = f"No location found in artifact dict. Keys present: {sorted(source)}"
        raise URIError(msg)

    for attr in ("value", "url", "location", "path"):
        candidate = getattr(source, attr, None)
        if isinstance(candidate, str) and candidate:
            return candidate

    msg = f"Cannot extract a location from {type(source).__name__}"
    raise URIError(msg)


def classify(location: str) -> str:
    """Work out what kind of location this is, without touching the network."""
    if location.startswith("beeble://"):
        return SCHEME_BEEBLE
    if location.startswith("data:"):
        return SCHEME_DATA

    parsed = urlparse(location)
    if parsed.scheme in ("http", "https"):
        host = (parsed.hostname or "").lower()
        if host in LOCAL_HOSTS:
            return SCHEME_LOCAL_URL
        return SCHEME_HTTPS

    return SCHEME_PATH


def is_reachable_by_beeble(location: str) -> bool:
    """Whether Beeble could fetch this as-is.

    A localhost URL is the canonical false case -- it resolves fine here and not at all there.
    """
    return classify(location) in (SCHEME_BEEBLE, SCHEME_DATA, SCHEME_HTTPS)


def filename_for(location: str, override: str | None = None) -> str:
    """Derive an upload filename, falling back to the URL/path basename.

    Raises:
        URIError: If the result would be rejected by INVALID_FILENAME.
    """
    if override:
        name = override
    else:
        name = Path(urlparse(location).path).name or "upload.bin"

    validate_filename(name)
    return name


def validate_filename(name: str) -> None:
    """Guard the documented filename rules locally, rather than paying a round trip to learn them.

    Raises:
        URIError: If the filename violates length or extension rules.
    """
    if not (FILENAME_MIN_CHARS <= len(name) <= FILENAME_MAX_CHARS):
        msg = (
            f"INVALID_FILENAME: {name!r} is {len(name)} characters; must be {FILENAME_MIN_CHARS}-{FILENAME_MAX_CHARS}."
        )
        raise URIError(msg)

    suffix = Path(name).suffix.lower()
    if suffix not in UPLOAD_EXTENSIONS:
        msg = f"INVALID_FILENAME: extension {suffix or '(none)'!r} not in {sorted(UPLOAD_EXTENSIONS)}."
        raise URIError(msg)


def content_type_for(name: str) -> str:
    guessed, _ = mimetypes.guess_type(name)
    return guessed or "application/octet-stream"


def data_uri_capacity_exceeded(size_bytes: int) -> bool:
    """Whether inlining these bytes would blow the 50 MB data: cap.

    The docs do not say whether the cap is pre- or post-base64. Assume post -- the pessimistic
    reading -- so roughly 37 MB of real file fits.
    """
    return size_bytes * BASE64_INFLATION > DATA_URI_MAX_BYTES


def to_data_uri(data: bytes, content_type: str) -> str:
    """Inline bytes as a data: URI.

    Raises:
        URIError: If the encoded payload exceeds the documented cap.
    """
    if data_uri_capacity_exceeded(len(data)):
        limit_mb = DATA_URI_MAX_BYTES / 1_048_576
        actual_mb = len(data) * BASE64_INFLATION / 1_048_576
        msg = (
            f"Encoded payload is ~{actual_mb:.1f} MB, over the {limit_mb:.0f} MB data: cap. "
            f"Use strategy={STRATEGY_UPLOAD!r} instead."
        )
        raise URIError(msg)

    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{content_type};base64,{encoded}"


async def read_bytes(location: str, client: BeebleClient) -> bytes:
    """Read the bytes behind a location, whether it is a URL or a local path.

    Raises:
        URIError: If a local path does not exist.
    """
    scheme = classify(location)

    if scheme in (SCHEME_LOCAL_URL, SCHEME_HTTPS):
        return await client.download(location)

    if scheme == SCHEME_DATA:
        _, _, payload = location.partition("base64,")
        if not payload:
            msg = "Only base64-encoded data: URIs are supported."
            raise URIError(msg)
        return base64.b64decode(payload)

    path = Path(location)
    if not path.is_file():
        msg = f"Not a readable file: {location}"
        raise URIError(msg)
    return path.read_bytes()


async def resolve(
    source: Any,
    client: BeebleClient,
    *,
    strategy: str = STRATEGY_UPLOAD,
    filename_override: str | None = None,
) -> ResolvedURI:
    """Turn a source into a URI Beeble can fetch.

    ``upload`` (default) downloads the bytes and PUTs them to a presigned URL. ``base64`` inlines
    them, which is capped at 50 MB. ``passthrough`` asserts the location is already reachable and
    fails loudly if it is not, rather than letting SOURCE_UNREACHABLE surface later.

    Raises:
        URIError: On an unknown strategy, or an unreachable passthrough.
    """
    if strategy not in STRATEGIES:
        msg = f"Unknown strategy {strategy!r}; expected one of {list(STRATEGIES)}."
        raise URIError(msg)

    location = extract_location(source)
    scheme = classify(location)

    # Already a Beeble URI: nothing to do under any strategy.
    if scheme == SCHEME_BEEBLE:
        return ResolvedURI(uri=location, scheme=scheme)

    if strategy == STRATEGY_PASSTHROUGH:
        if not is_reachable_by_beeble(location):
            msg = (
                f"passthrough refused: {location[:120]!r} is not reachable by Beeble "
                f"(classified as {scheme}). This is what SOURCE_UNREACHABLE looks like before it "
                f"costs you a round trip. Use strategy={STRATEGY_UPLOAD!r}."
            )
            raise URIError(msg)
        return ResolvedURI(uri=location, scheme=scheme)

    # A public https URL is already fetchable; re-uploading it wastes time and bandwidth.
    if scheme == SCHEME_HTTPS and strategy == STRATEGY_UPLOAD:
        return ResolvedURI(uri=location, scheme=scheme)

    if scheme == SCHEME_DATA and strategy == STRATEGY_BASE64:
        return ResolvedURI(uri=location, scheme=scheme)

    data = await read_bytes(location, client)

    if strategy == STRATEGY_BASE64:
        name = filename_override or Path(urlparse(location).path).name or "upload.bin"
        return ResolvedURI(
            uri=to_data_uri(data, content_type_for(name)),
            scheme=scheme,
            size_bytes=len(data),
        )

    name = filename_for(location, filename_override)
    created = await client.create_upload(name)
    upload_url = created.get("upload_url")
    beeble_uri = created.get("beeble_uri")
    if not isinstance(upload_url, str) or not isinstance(beeble_uri, str):
        msg = f"POST /uploads did not return upload_url and beeble_uri. Got keys: {sorted(created)}"
        raise URIError(msg)

    await client.put_upload(upload_url, data, content_type_for(name))
    return ResolvedURI(uri=beeble_uri, scheme=scheme, size_bytes=len(data), uploaded=True)
