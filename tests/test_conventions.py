"""Repo-wide guards for conventions that are easy to get wrong and expensive to debug.

These are text scans, not imports, so they cover spike/ too -- which imports griptape_nodes and
therefore cannot be imported inside this venv.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIRS = ("beeble_library", "spike")


EXCLUDED_PARTS = frozenset({".venv", "venv", "site-packages", "__pycache__", ".git"})


def python_files() -> list[Path]:
    """Our own sources only.

    The engine creates a per-library .venv inside the library directory, so an unfiltered rglob
    walks into site-packages and scans third-party code.
    """
    return [
        path
        for directory in SOURCE_DIRS
        for path in (REPO_ROOT / directory).rglob("*.py")
        if not EXCLUDED_PARTS & set(path.parts)
    ]


def test_python_files_were_found() -> None:
    # Guard the guard: a bad glob would make every scan below vacuously pass.
    assert len(python_files()) >= 5


def test_is_cancellation_requested_is_never_called() -> None:
    """It is a @property on BaseNode (node_types.py:368), not a method.

    Calling it raises "'bool' object is not callable" at runtime, and only at runtime -- which cost
    us a failed node execution. CLAUDE.md and DESIGN.md both documented it with parens.
    """
    offenders = [
        f"{path.relative_to(REPO_ROOT)}:{number}"
        for path in python_files()
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if "is_cancellation_requested()" in line
    ]
    assert offenders == [], f"is_cancellation_requested is a property, drop the parens: {offenders}"


def test_only_client_imports_httpx() -> None:
    """client.py is the seam for MockTransport tests and the single home of retry/backoff."""
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path in (REPO_ROOT / "beeble_library").rglob("*.py")
        if path.name != "client.py" and "import httpx" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_no_blocking_sleep_or_requests() -> None:
    """Blocking calls stall the engine event loop; requests has no place in an async node."""
    offenders = [
        f"{path.relative_to(REPO_ROOT)}:{number}"
        for path in python_files()
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if "time.sleep(" in line or line.strip().startswith(("import requests", "from requests"))
    ]
    assert offenders == [], f"use asyncio.sleep and httpx: {offenders}"


def test_poll_interval_never_defaults_below_the_read_cap() -> None:
    """5 RPM reads = one request per 12 s. Beeble's own quickstart uses 5 s; do not copy it."""
    from beeble_library.constants import DEFAULT_POLL_INTERVAL_SECONDS, MIN_POLL_INTERVAL_SECONDS

    assert MIN_POLL_INTERVAL_SECONDS == 12
    assert DEFAULT_POLL_INTERVAL_SECONDS >= MIN_POLL_INTERVAL_SECONDS
