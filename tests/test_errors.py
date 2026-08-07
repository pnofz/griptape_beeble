"""One case per documented error code, plus the envelope parsing."""

from __future__ import annotations

import pytest

from beeble_library.errors import (
    DOCUMENTED_CODE_COUNT,
    ERRORS,
    BeebleError,
    RetryPolicy,
    describe,
    from_response,
)

# Every code Beeble documents, grouped by the status it is returned under.
EXPECTED_BY_STATUS = {
    400: [
        "INVALID_GENERATION_TYPE",
        "INVALID_ALPHA_MODE",
        "INVALID_MAX_RESOLUTION",
        "MISSING_STYLE_INPUT",
        "MISSING_SOURCE",
        "MISSING_ALPHA",
        "INVALID_FILE_FORMAT",
        "ALPHA_TYPE_MISMATCH",
        "ALPHA_MUST_BE_IMAGE",
        "SOURCE_TOO_LARGE",
        "VIDEO_TOO_MANY_FRAMES",
        "INVALID_URI",
        "SOURCE_UNREACHABLE",
        "INVALID_FILENAME",
        "INVALID_CALLBACK_URL",
    ],
    401: ["INVALID_API_KEY"],
    402: ["BILLING_NOT_CONFIGURED", "INSUFFICIENT_BALANCE", "HARD_LIMIT_EXCEEDED", "CREDIT_DEDUCTION_FAILED"],
    404: ["JOB_NOT_FOUND"],
    429: ["RATE_LIMIT_EXCEEDED", "CONCURRENT_LIMIT_EXCEEDED"],
    500: ["INTERNAL_ERROR", "CREDIT_DEDUCTION_FAILED", "UPLOAD_URL_FAILED", "JOB_QUEUE_FAILED"],
}

ALL_CODES = sorted({code for codes in EXPECTED_BY_STATUS.values() for code in codes})


def test_all_documented_codes_are_mapped() -> None:
    assert sorted(ERRORS) == ALL_CODES


def test_unique_code_count_matches_documentation() -> None:
    # 27 documented entries, 26 unique: CREDIT_DEDUCTION_FAILED appears under both 402 and 500.
    entry_count = sum(len(codes) for codes in EXPECTED_BY_STATUS.values())
    assert entry_count == 27
    assert len(ALL_CODES) == DOCUMENTED_CODE_COUNT == 26


@pytest.mark.parametrize(("status", "codes"), EXPECTED_BY_STATUS.items())
def test_codes_declare_their_http_status(status: int, codes: list[str]) -> None:
    for code in codes:
        assert status in ERRORS[code].http_status, f"{code} should be documented under {status}"


def test_credit_deduction_failed_spans_two_statuses() -> None:
    info = ERRORS["CREDIT_DEDUCTION_FAILED"]
    assert set(info.http_status) == {402, 500}
    # It is the one 402-coded error worth retrying, because it is server-side.
    assert info.retry == RetryPolicy.BACKOFF


@pytest.mark.parametrize("code", ALL_CODES)
def test_every_code_has_an_actionable_remedy(code: str) -> None:
    info = ERRORS[code]
    assert info.message, f"{code} is missing Beeble's documented message"
    assert len(info.remedy) > 20, f"{code} needs a remedy that tells the user what to do"


@pytest.mark.parametrize(
    ("code", "expected_node"),
    [
        ("SOURCE_TOO_LARGE", "Fit To Pixel Budget"),
        ("VIDEO_TOO_MANY_FRAMES", "Trim To Frame Limit"),
        ("SOURCE_UNREACHABLE", "Resolve URI"),
        ("INVALID_URI", "Resolve URI"),
        ("ALPHA_MUST_BE_IMAGE", "Keyframe Alpha"),
        ("ALPHA_TYPE_MISMATCH", "Alpha From Matte"),
        ("HARD_LIMIT_EXCEEDED", "Spend Guard"),
    ],
)
def test_remedies_name_the_fixing_node(code: str, expected_node: str) -> None:
    assert expected_node in ERRORS[code].remedy


def test_billing_errors_are_never_retried() -> None:
    for code in ("BILLING_NOT_CONFIGURED", "INSUFFICIENT_BALANCE", "HARD_LIMIT_EXCEEDED"):
        assert ERRORS[code].retry == RetryPolicy.NEVER, f"{code} must not be retried"


def test_concurrency_waits_for_a_slot_rather_than_backing_off() -> None:
    assert ERRORS["CONCURRENT_LIMIT_EXCEEDED"].retry == RetryPolicy.AWAIT_SLOT
    assert ERRORS["RATE_LIMIT_EXCEEDED"].retry == RetryPolicy.BACKOFF


def test_describe_includes_code_message_and_remedy() -> None:
    text = describe("SOURCE_TOO_LARGE", "Source resolution exceeds 2,770,000 total pixels", http_status=400)
    assert "HTTP 400" in text
    assert "SOURCE_TOO_LARGE" in text
    assert "Fit To Pixel Budget" in text


def test_describe_degrades_gracefully_for_unknown_codes() -> None:
    text = describe("SOME_FUTURE_CODE", "who knows", http_status=400)
    assert "SOME_FUTURE_CODE" in text
    assert "who knows" in text


def test_describe_handles_a_missing_code() -> None:
    assert "UNKNOWN_ERROR" in describe(None, "")


def test_from_response_parses_the_documented_envelope() -> None:
    error = from_response({"error": {"code": "SOURCE_TOO_LARGE", "message": "too big"}}, 400)
    assert error.code == "SOURCE_TOO_LARGE"
    assert error.http_status == 400
    assert error.is_retryable is False
    assert "Fit To Pixel Budget" in str(error)


def test_from_response_survives_a_malformed_envelope() -> None:
    for payload in ({}, {"error": None}, {"error": "boom"}, {"unexpected": 1}):
        error = from_response(payload, 500)
        assert isinstance(error, BeebleError)
        assert error.is_retryable is False


def test_retryable_flag_follows_policy() -> None:
    assert from_response({"error": {"code": "RATE_LIMIT_EXCEEDED"}}, 429).is_retryable is True
    assert from_response({"error": {"code": "INVALID_API_KEY"}}, 401).is_retryable is False
