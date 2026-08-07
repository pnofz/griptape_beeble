"""URI resolution: localhost, https, beeble://, data: and local paths."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import httpx
import pytest

from beeble_library import client as client_mod
from beeble_library.client import BeebleClient, reset_buckets
from beeble_library.uri import (
    SCHEME_BEEBLE,
    SCHEME_DATA,
    SCHEME_HTTPS,
    SCHEME_LOCAL_URL,
    SCHEME_PATH,
    STRATEGY_BASE64,
    STRATEGY_PASSTHROUGH,
    STRATEGY_UPLOAD,
    URIError,
    classify,
    content_type_for,
    data_uri_capacity_exceeded,
    extract_location,
    filename_for,
    is_reachable_by_beeble,
    resolve,
    to_data_uri,
    validate_filename,
)


@pytest.fixture(autouse=True)
def _fast_buckets() -> None:
    reset_buckets(read_rpm=10_000, write_rpm=10_000)


def make_client(handler: Any) -> BeebleClient:
    return BeebleClient("test-key", transport=httpx.MockTransport(handler))


# -- classification ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("beeble://uploads/upload_1/source.mp4", SCHEME_BEEBLE),
        ("data:video/mp4;base64,AAAA", SCHEME_DATA),
        ("https://cdn.beeble.ai/public/source.mp4", SCHEME_HTTPS),
        ("http://example.com/source.mp4", SCHEME_HTTPS),
        ("http://localhost:8124/static/source.mp4", SCHEME_LOCAL_URL),
        ("http://127.0.0.1:8124/static/source.mp4", SCHEME_LOCAL_URL),
        ("http://0.0.0.0:8124/x.mp4", SCHEME_LOCAL_URL),
        ("C:/renders/plate.mp4", SCHEME_PATH),
        ("/mnt/renders/plate.mp4", SCHEME_PATH),
        ("relative/plate.mp4", SCHEME_PATH),
    ],
)
def test_classify(location: str, expected: str) -> None:
    assert classify(location) == expected


def test_localhost_is_not_reachable_by_beeble() -> None:
    # This is the single most common integration bug, so it gets its own assertion.
    assert is_reachable_by_beeble("http://localhost:8124/static/a.mp4") is False
    assert is_reachable_by_beeble("https://cdn.beeble.ai/a.mp4") is True
    assert is_reachable_by_beeble("beeble://uploads/u1/a.mp4") is True
    assert is_reachable_by_beeble("data:image/png;base64,AA") is True


# -- location extraction ----------------------------------------------------------------------


def test_extract_location_from_string_and_path() -> None:
    assert extract_location("https://x/a.mp4") == "https://x/a.mp4"
    assert extract_location(Path("/tmp/a.mp4")) == str(Path("/tmp/a.mp4"))


@pytest.mark.parametrize("key", ["value", "url", "location", "path", "uri"])
def test_extract_location_from_serialized_artifact_dict(key: str) -> None:
    # Serialized artifacts arrive as dicts, not objects.
    assert extract_location({key: "http://localhost:8124/a.mp4"}) == "http://localhost:8124/a.mp4"


def test_extract_location_from_object_attribute() -> None:
    class Artifact:
        value = "https://x/a.mp4"

    assert extract_location(Artifact()) == "https://x/a.mp4"


def test_extract_location_reports_what_it_saw() -> None:
    with pytest.raises(URIError) as excinfo:
        extract_location({"unexpected": 1})
    assert "unexpected" in str(excinfo.value)


# -- filenames --------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["a.mp4", "plate.mov", "ref.png", "ref.jpg", "ref.jpeg", "ref.webp"])
def test_valid_filenames(name: str) -> None:
    validate_filename(name)


@pytest.mark.parametrize(
    ("name", "reason"), [("ab", "3-255"), ("a" * 300 + ".mp4", "3-255"), ("plate.exr", "extension")]
)
def test_invalid_filenames_explain_themselves(name: str, reason: str) -> None:
    with pytest.raises(URIError) as excinfo:
        validate_filename(name)
    assert "INVALID_FILENAME" in str(excinfo.value)
    assert reason in str(excinfo.value)


def test_filename_derived_from_url_path() -> None:
    assert filename_for("http://localhost:8124/static/plate.mp4") == "plate.mp4"


def test_filename_override_wins() -> None:
    assert filename_for("http://localhost:8124/static/plate.mp4", "shot_010.mp4") == "shot_010.mp4"


def test_content_type_guessing() -> None:
    assert content_type_for("a.mp4") == "video/mp4"
    assert content_type_for("a.png") == "image/png"
    assert content_type_for("a.unknown") == "application/octet-stream"


# -- data: URIs -------------------------------------------------------------------------------


def test_to_data_uri_round_trips() -> None:
    uri = to_data_uri(b"hello", "video/mp4")
    assert uri.startswith("data:video/mp4;base64,")
    assert base64.b64decode(uri.partition("base64,")[2]) == b"hello"


def test_data_uri_cap_assumes_post_base64_inflation() -> None:
    # 40 MB of real file inflates past the 50 MB cap once encoded.
    assert data_uri_capacity_exceeded(40 * 1024 * 1024) is True
    assert data_uri_capacity_exceeded(30 * 1024 * 1024) is False


def test_to_data_uri_refuses_oversized_payloads() -> None:
    with pytest.raises(URIError) as excinfo:
        to_data_uri(b"x" * (40 * 1024 * 1024), "video/mp4")
    assert "upload" in str(excinfo.value)


# -- resolve ----------------------------------------------------------------------------------


async def test_beeble_uri_passes_through_untouched() -> None:
    async with make_client(lambda _r: httpx.Response(500)) as client:
        result = await resolve("beeble://uploads/u1/a.mp4", client)

    assert result.uri == "beeble://uploads/u1/a.mp4"
    assert result.uploaded is False


async def test_localhost_artifact_is_downloaded_and_uploaded() -> None:
    """The whole reason this module exists."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "localhost":
            return httpx.Response(200, content=b"plate-bytes")
        if request.url.path.endswith("/uploads"):
            seen["filename"] = request.read().decode()
            return httpx.Response(
                200,
                json={
                    "id": "upload_1",
                    "upload_url": "https://storage.example/put",
                    "beeble_uri": "beeble://uploads/upload_1/plate.mp4",
                },
            )
        seen["put_body"] = request.read()
        seen["put_type"] = request.headers.get("content-type")
        return httpx.Response(200)

    async with make_client(handler) as client:
        result = await resolve({"value": "http://localhost:8124/static/plate.mp4"}, client)

    assert result.uri == "beeble://uploads/upload_1/plate.mp4"
    assert result.uploaded is True
    assert result.size_bytes == len(b"plate-bytes")
    assert seen["put_body"] == b"plate-bytes"
    assert seen["put_type"] == "video/mp4"
    assert "plate.mp4" in seen["filename"]


async def test_public_https_is_not_needlessly_re_uploaded() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        msg = "should not have made any request"
        raise AssertionError(msg)

    async with make_client(handler) as client:
        result = await resolve("https://cdn.beeble.ai/public/source.mp4", client)

    assert result.uri == "https://cdn.beeble.ai/public/source.mp4"
    assert result.uploaded is False


async def test_base64_strategy_inlines_localhost_bytes() -> None:
    async with make_client(lambda _r: httpx.Response(200, content=b"abc")) as client:
        result = await resolve(
            "http://localhost:8124/static/plate.mp4",
            client,
            strategy=STRATEGY_BASE64,
        )

    assert result.uri.startswith("data:video/mp4;base64,")
    assert base64.b64decode(result.uri.partition("base64,")[2]) == b"abc"
    assert result.uploaded is False


async def test_passthrough_refuses_a_localhost_url() -> None:
    async with make_client(lambda _r: httpx.Response(200)) as client:
        with pytest.raises(URIError) as excinfo:
            await resolve("http://localhost:8124/a.mp4", client, strategy=STRATEGY_PASSTHROUGH)

    message = str(excinfo.value)
    assert "SOURCE_UNREACHABLE" in message
    assert STRATEGY_UPLOAD in message


async def test_passthrough_allows_a_public_url() -> None:
    async with make_client(lambda _r: httpx.Response(200)) as client:
        result = await resolve("https://cdn.beeble.ai/a.mp4", client, strategy=STRATEGY_PASSTHROUGH)
    assert result.uri == "https://cdn.beeble.ai/a.mp4"


async def test_local_file_is_uploaded(tmp_path: Path) -> None:
    plate = tmp_path / "shot.mp4"
    plate.write_bytes(b"local-bytes")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/uploads"):
            return httpx.Response(
                200,
                json={
                    "id": "upload_2",
                    "upload_url": "https://storage.example/put",
                    "beeble_uri": "beeble://uploads/upload_2/shot.mp4",
                },
            )
        return httpx.Response(200)

    async with make_client(handler) as client:
        result = await resolve(plate, client)

    assert result.uri == "beeble://uploads/upload_2/shot.mp4"
    assert result.size_bytes == len(b"local-bytes")


async def test_missing_local_file_is_reported() -> None:
    async with make_client(lambda _r: httpx.Response(200)) as client:
        with pytest.raises(URIError):
            await resolve("/definitely/not/here.mp4", client)


async def test_unknown_strategy_is_rejected() -> None:
    async with make_client(lambda _r: httpx.Response(200)) as client:
        with pytest.raises(URIError) as excinfo:
            await resolve("https://x/a.mp4", client, strategy="teleport")
    assert "teleport" in str(excinfo.value)


async def test_malformed_upload_response_is_reported() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "localhost":
            return httpx.Response(200, content=b"bytes")
        return httpx.Response(200, json={"id": "upload_3"})  # no upload_url / beeble_uri

    async with make_client(handler) as client:
        with pytest.raises(URIError) as excinfo:
            await resolve("http://localhost:8124/a.mp4", client)

    assert "upload_url" in str(excinfo.value)


async def test_upload_rejects_an_unsupported_extension() -> None:
    async with make_client(lambda _r: httpx.Response(200, content=b"x")) as client:
        with pytest.raises(URIError) as excinfo:
            await resolve("http://localhost:8124/static/plate.exr", client)

    assert "INVALID_FILENAME" in str(excinfo.value)


def test_client_module_is_the_only_httpx_importer() -> None:
    """CLAUDE.md: only client.py imports httpx. It is the seam for mock transport."""
    library = Path(client_mod.__file__).parent
    offenders = [
        path.name
        for path in library.glob("*.py")
        if path.name != "client.py" and "import httpx" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
