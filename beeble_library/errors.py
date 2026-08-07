"""Beeble error codes mapped to actionable messages.

The OpenAPI spec declares only 200 responses, so the error envelope exists in prose only:
``{"error": {"message": ..., "code": ...}}``. This module supplies the model the generated client
would not have.

27 documented entries, 26 unique codes -- ``CREDIT_DEDUCTION_FAILED`` is documented under both 402
and 500. Every message names the node or action that fixes the problem, because a bare
"SOURCE_TOO_LARGE" tells an artist nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

# --- retry policy ----------------------------------------------------------------------------


class RetryPolicy:
    """How the client should react to a given error code."""

    NEVER = "never"
    """Terminal. Retrying spends time and changes nothing."""

    BACKOFF = "backoff"
    """Transient. Retry with exponential backoff."""

    AWAIT_SLOT = "await_slot"
    """Concurrency ceiling. Do NOT back off blindly -- poll active jobs and wait for a slot."""


@dataclass(frozen=True)
class BeebleErrorInfo:
    """One documented error code."""

    code: str
    http_status: tuple[int, ...]
    message: str
    """Beeble's own documented message, verbatim."""
    remedy: str
    """What the user should actually do about it."""
    retry: str = RetryPolicy.NEVER


def _e(
    code: str, status: tuple[int, ...], message: str, remedy: str, retry: str = RetryPolicy.NEVER
) -> BeebleErrorInfo:
    return BeebleErrorInfo(code=code, http_status=status, message=message, remedy=remedy, retry=retry)


ERRORS: Final[dict[str, BeebleErrorInfo]] = {
    # --- 400 validation ---------------------------------------------------------------------
    "INVALID_GENERATION_TYPE": _e(
        "INVALID_GENERATION_TYPE",
        (400,),
        "generation_type must be image or video",
        "Use the SwitchX Video or SwitchX Image node, which set generation_type for you.",
    ),
    "INVALID_ALPHA_MODE": _e(
        "INVALID_ALPHA_MODE",
        (400,),
        "alpha_mode must be auto, fill, custom, or select",
        "Set alpha_mode from the Alpha Config dropdown rather than by hand.",
    ),
    "INVALID_MAX_RESOLUTION": _e(
        "INVALID_MAX_RESOLUTION",
        (400,),
        "max_resolution must be 720 or 1080",
        "Set max_resolution to 720 or 1080. The schema types it as a bare integer, so this is only "
        "caught server-side -- Validate Request guards it locally.",
    ),
    "MISSING_STYLE_INPUT": _e(
        "MISSING_STYLE_INPUT",
        (400,),
        "Neither reference_image_uri nor prompt was provided",
        "Supply a prompt or connect a reference image on Generation Config. One of the two is required.",
    ),
    "MISSING_SOURCE": _e(
        "MISSING_SOURCE",
        (400,),
        "source_uri is required",
        "Connect a source into the node, or insert Resolve URI upstream to produce a source_uri.",
    ),
    "MISSING_ALPHA": _e(
        "MISSING_ALPHA",
        (400,),
        "alpha_uri required when alpha_mode is custom or select",
        "Connect an alpha into Alpha Config, or switch alpha_mode to auto or fill.",
    ),
    "INVALID_FILE_FORMAT": _e(
        "INVALID_FILE_FORMAT",
        (400,),
        "Source format doesn't match generation_type",
        "A video source needs SwitchX Video; an image source needs SwitchX Image. Inspect Media "
        "reports which one you actually have.",
    ),
    "ALPHA_TYPE_MISMATCH": _e(
        "ALPHA_TYPE_MISMATCH",
        (400,),
        "Alpha type doesn't match source when alpha_mode is custom",
        "With alpha_mode=custom the alpha must be the same type as the source (video alpha for a "
        "video source). Use Alpha From Matte to build a matching alpha.",
    ),
    "ALPHA_MUST_BE_IMAGE": _e(
        "ALPHA_MUST_BE_IMAGE",
        (400,),
        "Alpha must be an image (PNG/JPG) when alpha_mode is select",
        "With alpha_mode=select the alpha must be a grayscale image. Use Keyframe Alpha to pull a "
        "single frame out of a matte sequence.",
    ),
    "SOURCE_TOO_LARGE": _e(
        "SOURCE_TOO_LARGE",
        (400,),
        "Source resolution exceeds 2,770,000 total pixels",
        "Insert Fit To Pixel Budget upstream. Note this caps the SOURCE -- it is a different ceiling "
        "from max_resolution, which caps the output.",
    ),
    "VIDEO_TOO_MANY_FRAMES": _e(
        "VIDEO_TOO_MANY_FRAMES",
        (400,),
        "Video exceeds the maximum of 240 frames",
        "Insert Trim To Frame Limit upstream, or split the plate with Split To Chunks.",
    ),
    "INVALID_URI": _e(
        "INVALID_URI",
        (400,),
        "URI format invalid or file type could not be determined",
        "URIs must be beeble://, https:// or data:. Insert Resolve URI to produce a valid one.",
    ),
    "SOURCE_UNREACHABLE": _e(
        "SOURCE_UNREACHABLE",
        (400,),
        "Source, alpha, or reference URI is not reachable",
        "Beeble cannot fetch the URI. Upstream artifacts are localhost URLs -- insert Resolve URI to "
        "upload the bytes and hand Beeble a reachable URI.",
    ),
    "INVALID_FILENAME": _e(
        "INVALID_FILENAME",
        (400,),
        "Filename invalid or unsupported extension",
        "Filenames must be 3-255 characters and end in .mp4 .mov .png .jpg .jpeg or .webp. Set "
        "filename_override on Beeble Upload.",
    ),
    "INVALID_CALLBACK_URL": _e(
        "INVALID_CALLBACK_URL",
        (400,),
        "Callback URL is not valid HTTPS or is unreachable",
        "callback_url must be HTTPS and reachable from Beeble. Clear it on Generation Config to poll in-graph instead.",
    ),
    # --- 401 auth ---------------------------------------------------------------------------
    "INVALID_API_KEY": _e(
        "INVALID_API_KEY",
        (401,),
        "API key is missing or invalid",
        "Set BEEBLE_API_KEY under Settings -> API Keys & Secrets.",
    ),
    # --- 402 billing ------------------------------------------------------------------------
    "BILLING_NOT_CONFIGURED": _e(
        "BILLING_NOT_CONFIGURED",
        (402,),
        "No billing account or no active API subscription",
        "Set up billing in the Beeble dashboard. Never retried -- nothing about this improves on its own.",
    ),
    "INSUFFICIENT_BALANCE": _e(
        "INSUFFICIENT_BALANCE",
        (402,),
        "Prepaid account balance is too low for this job",
        "Top up the prepaid balance in the Beeble dashboard. Use Spend Guard upstream to stop a batch "
        "before it hits this.",
    ),
    "HARD_LIMIT_EXCEEDED": _e(
        "HARD_LIMIT_EXCEEDED",
        (402,),
        "Job would push current period usage over spending limit",
        "Raise the spending limit in the Beeble dashboard, or wait for the next billing period. "
        "Spend Guard catches this before submit.",
    ),
    "CREDIT_DEDUCTION_FAILED": _e(
        "CREDIT_DEDUCTION_FAILED",
        (402, 500),
        "Failed to process credit deduction (server-side issue)",
        "Server-side fault, not a billing problem on your end. Retried automatically with backoff. "
        "Documented under both 402 and 500 -- the one 402-coded error that is worth retrying.",
        retry=RetryPolicy.BACKOFF,
    ),
    # --- 404 --------------------------------------------------------------------------------
    "JOB_NOT_FOUND": _e(
        "JOB_NOT_FOUND",
        (404,),
        "Job doesn't exist or is not owned by you",
        "Check the job id. Jobs are scoped to the account that created them, so a key change orphans them.",
    ),
    # --- 429 rate limiting ------------------------------------------------------------------
    "RATE_LIMIT_EXCEEDED": _e(
        "RATE_LIMIT_EXCEEDED",
        (429,),
        "Too many requests per minute",
        "Raise poll_interval (15 s minimum, higher with concurrent waiters) or lower batch "
        "concurrency. Retried automatically with exponential backoff.",
        retry=RetryPolicy.BACKOFF,
    ),
    "CONCURRENT_LIMIT_EXCEEDED": _e(
        "CONCURRENT_LIMIT_EXCEEDED",
        (429,),
        "Too many in-flight generation jobs",
        "Too many jobs in flight. Lower max_concurrent on Batch Submit. The client waits for a free "
        "slot rather than backing off blindly -- backing off does not free a slot.",
        retry=RetryPolicy.AWAIT_SLOT,
    ),
    # --- 500 server -------------------------------------------------------------------------
    "INTERNAL_ERROR": _e(
        "INTERNAL_ERROR",
        (500,),
        "Unexpected server error",
        "Server-side fault. Retried automatically with backoff; if it persists, contact Beeble support.",
        retry=RetryPolicy.BACKOFF,
    ),
    "UPLOAD_URL_FAILED": _e(
        "UPLOAD_URL_FAILED",
        (500,),
        "Failed to generate upload URL",
        "Server-side fault while minting a presigned upload URL. Retried automatically with backoff.",
        retry=RetryPolicy.BACKOFF,
    ),
    "JOB_QUEUE_FAILED": _e(
        "JOB_QUEUE_FAILED",
        (500,),
        "Failed to queue job for processing",
        "Server-side fault before the job was queued, so nothing was spent. Retried automatically with backoff.",
        retry=RetryPolicy.BACKOFF,
    ),
}

DOCUMENTED_CODE_COUNT: Final = 26
"""Unique codes. There are 27 documented entries; CREDIT_DEDUCTION_FAILED appears under 402 and 500."""


class BeebleError(Exception):
    """An error returned by the Beeble API.

    Carries the parsed code so callers can branch on retry policy without string matching.
    """

    def __init__(
        self,
        code: str | None,
        message: str,
        *,
        http_status: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.api_message = message
        self.http_status = http_status
        self.payload = payload or {}
        self.info = ERRORS.get(code) if code else None
        super().__init__(describe(code, message, http_status=http_status))

    @property
    def retry(self) -> str:
        return self.info.retry if self.info else RetryPolicy.NEVER

    @property
    def is_retryable(self) -> bool:
        return self.retry != RetryPolicy.NEVER


def describe(code: str | None, message: str = "", *, http_status: int | None = None) -> str:
    """Build an actionable message for an error code.

    Unknown codes degrade gracefully -- Beeble may add codes faster than this module tracks them.
    """
    info = ERRORS.get(code) if code else None
    status = f"HTTP {http_status}: " if http_status is not None else ""

    if info is None:
        label = code or "UNKNOWN_ERROR"
        detail = message or "No message supplied by the API."
        return f"{status}{label}: {detail}"

    detail = message or info.message
    return f"{status}{info.code}: {detail} -> {info.remedy}"


def from_response(payload: dict[str, Any], http_status: int | None = None) -> BeebleError:
    """Build a BeebleError from a parsed ``{"error": {...}}`` response body.

    Tolerates a missing or malformed envelope, since a 500 may not return JSON at all.
    """
    envelope = payload.get("error")
    if isinstance(envelope, dict):
        code = envelope.get("code")
        message = envelope.get("message", "")
    elif isinstance(envelope, str):
        # Some server errors collapse the envelope to a bare string.
        code, message = None, envelope
    else:
        code, message = None, ""

    return BeebleError(
        code if isinstance(code, str) else None,
        message if isinstance(message, str) else "",
        http_status=http_status,
        payload=payload,
    )
