"""Hard limits and enums from the Beeble SwitchX API.

Every value here was checked against the OpenAPI schema and the published docs. Do not re-derive
them; see CLAUDE.md "Hard API facts".
"""

from __future__ import annotations

from typing import Final, Literal

API_BASE_URL: Final = "https://api.beeble.ai/v1"
API_KEY_HEADER: Final = "x-api-key"
API_KEY_NAME: Final = "BEEBLE_API_KEY"

# --- request enums -------------------------------------------------------------------------

GenerationType = Literal["image", "video"]
AlphaMode = Literal["auto", "fill", "custom", "select"]
JobStatus = Literal["in_queue", "processing", "completed", "failed"]
WebhookStatus = Literal["pending", "delivered", "failed"]

GENERATION_TYPES: Final[tuple[str, ...]] = ("image", "video")
ALPHA_MODES: Final[tuple[str, ...]] = ("auto", "fill", "custom", "select")
JOB_STATUSES: Final[tuple[str, ...]] = ("in_queue", "processing", "completed", "failed")
TERMINAL_STATUSES: Final[frozenset[str]] = frozenset({"completed", "failed"})

# alpha_uri is required for these two modes.
ALPHA_MODES_REQUIRING_URI: Final[frozenset[str]] = frozenset({"custom", "select"})

# --- hard limits ---------------------------------------------------------------------------

PIXEL_BUDGET: Final = 2_770_000
"""Max source pixels (width x height). Caps the SOURCE, not the output."""

MAX_FRAMES: Final = 240
"""Max frames in a source video."""

MAX_PROMPT_CHARS: Final = 2000
SEED_MIN: Final = 0
SEED_MAX: Final = 4_294_967_295

MAX_RESOLUTIONS: Final[tuple[int, ...]] = (720, 1080)
DEFAULT_MAX_RESOLUTION: Final = 1080
"""Caps the OUTPUT. Different ceiling from PIXEL_BUDGET: 2560x1080 passes the source
budget (2.76 MP) while 2560x1440 (3.69 MP) does not."""

IDEMPOTENCY_KEY_MIN_CHARS: Final = 1
IDEMPOTENCY_KEY_MAX_CHARS: Final = 256

FILENAME_MIN_CHARS: Final = 3
FILENAME_MAX_CHARS: Final = 255
UPLOAD_EXTENSIONS: Final[frozenset[str]] = frozenset({".mp4", ".mov", ".png", ".jpg", ".jpeg", ".webp"})
IMAGE_EXTENSIONS: Final[frozenset[str]] = frozenset({".png", ".jpg", ".jpeg", ".webp"})
VIDEO_EXTENSIONS: Final[frozenset[str]] = frozenset({".mp4", ".mov"})

DATA_URI_MAX_BYTES: Final = 50 * 1024 * 1024
"""The docs do not say whether the 50 MB cap is pre- or post-base64. Assume post (pessimistic),
so roughly 37 MB of actual file survives the ~33% base64 inflation."""

BASE64_INFLATION: Final = 4 / 3

OUTPUT_URL_TTL_HOURS: Final = 72
"""Signed output URLs expire. Re-fetch by job id for fresh ones."""

# --- rate limits ---------------------------------------------------------------------------
#
# These are per-account DEFAULTS, not constants. Accounts go to 10,000 RPM and 100 concurrent.
# Read live from GET /v1/account/info; `rate_limits.*.limit == null` means default/unlimited,
# NOT zero. Never hardcode these in node logic - they exist only as a fallback.

DEFAULT_READ_RPM: Final = 5
DEFAULT_WRITE_RPM: Final = 5
DEFAULT_CONCURRENCY: Final = 10

MIN_POLL_INTERVAL_SECONDS: Final = 12
"""5 RPM reads = one request per 12 s. With N concurrent waiters at interval P you trip the
limiter when P < 12N."""

DEFAULT_POLL_INTERVAL_SECONDS: Final = 15
DEFAULT_TIMEOUT_MINUTES: Final = 20

LIST_LIMIT_MIN: Final = 1
LIST_LIMIT_MAX: Final = 100
LIST_LIMIT_DEFAULT: Final = 20

# --- billing -------------------------------------------------------------------------------

METER_IDS: Final[dict[tuple[str, int], str]] = {
    ("video", 1080): "api_video_1080p",
    ("video", 720): "api_video_720p",
    ("image", 1080): "api_image_1080p",
    ("image", 720): "api_image_720p",
}
"""Meter ids reported by GET /v1/account/billing. That endpoint carries total_usage but NO unit
price - live pricing needs dashboard bearer auth and is unreachable with an API key."""


def meter_id(generation_type: str, max_resolution: int) -> str:
    """Map a generation to its billing meter id.

    Raises:
        ValueError: If the combination is not a documented meter.
    """
    try:
        return METER_IDS[(generation_type, max_resolution)]
    except KeyError:
        msg = (
            f"No billing meter for generation_type={generation_type!r} max_resolution={max_resolution!r}. "
            f"Expected one of {sorted(METER_IDS)}."
        )
        raise ValueError(msg) from None
