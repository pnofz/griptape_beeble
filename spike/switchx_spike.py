"""Throwaway spike node: proves the whole Beeble SwitchX loop end to end.

Not part of the library. Its only job is to settle the open questions in CLAUDE.md against a
live engine and a live API for a few dollars of credit, then be deleted.

Deliberately self-contained — no beeble_library imports — so it can run before any of P0 exists.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any

import httpx
from griptape.artifacts import VideoUrlArtifact
from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import SuccessFailureNode
from griptape_nodes.exe_types.param_components.project_file_parameter import ProjectFileParameter
from griptape_nodes.exe_types.param_types.parameter_video import ParameterVideo
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

BASE_URL = "https://api.beeble.ai/v1"

# Beeble's public quickstart assets — already https-reachable, so the spike sidesteps the
# localhost-artifact problem (gotcha 6) and tests only the generation loop.
SAMPLE_SOURCE = "https://cdn.beeble.ai/public/developer-api/source.mp4"
SAMPLE_REFERENCE = "https://cdn.beeble.ai/public/developer-api/reference.png"
SAMPLE_ALPHA = "https://cdn.beeble.ai/public/developer-api/alpha.mp4"

# 5 RPM reads = one request per 12 s. 15 s is the floor the library will ship with.
# Beeble's own quickstart uses 5 s, which is 2.4x over the limit from a single waiter.
POLL_INTERVAL_SECONDS = 15
TIMEOUT_MINUTES = 20


class SwitchXSpike(SuccessFailureNode):
    """Submit one 720p video generation against Beeble's sample assets and download the render."""

    API_KEY_NAME = "BEEBLE_API_KEY"

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata=metadata)

        self.add_parameter(
            Parameter(
                name="prompt",
                type="str",
                default_value="cinematic golden hour rim light from camera left, warm practical fill",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
                tooltip="Style prompt. Sent alongside the sample reference image.",
                ui_options={"multiline": True},
            )
        )
        self.add_parameter(
            Parameter(
                name="poll_interval",
                type="int",
                default_value=POLL_INTERVAL_SECONDS,
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
                tooltip="Seconds between status polls. Do not go below 12 (5 RPM read cap).",
            )
        )

        # --- outputs -------------------------------------------------------------------
        self.add_parameter(
            ParameterVideo(
                name="output_video",
                tooltip="The downloaded render",
                allowed_modes={ParameterMode.OUTPUT},
                allow_input=False,
                allow_property=False,
            )
        )
        self.add_parameter(
            Parameter(
                name="progress",
                type="int",
                default_value=0,
                allowed_modes={ParameterMode.OUTPUT},
                tooltip="Job progress 0-100",
                ui_options={"progress_bar": True},
            )
        )
        self.add_parameter(
            Parameter(
                name="status",
                type="str",
                default_value="",
                allowed_modes={ParameterMode.OUTPUT},
                tooltip="in_queue / processing / completed / failed",
            )
        )
        self.add_parameter(
            Parameter(
                name="job_id",
                type="str",
                default_value="",
                allowed_modes={ParameterMode.OUTPUT},
                tooltip="swx_... id, so a timed-out job can be picked up later rather than lost",
            )
        )
        self.add_parameter(
            Parameter(
                name="findings",
                type="str",
                default_value="",
                allowed_modes={ParameterMode.OUTPUT},
                tooltip="What the spike observed, keyed to the open questions in CLAUDE.md",
                ui_options={"multiline": True},
            )
        )
        self.add_parameter(
            Parameter(
                name="raw_job",
                type="json",
                default_value={},
                allowed_modes={ParameterMode.OUTPUT},
                tooltip="Full final status response — the ground truth for undocumented fields",
            )
        )

        self._file_param = ProjectFileParameter(
            node=self,
            name="output_file",
            default_filename="switchx_spike.mp4",
        )
        self._file_param.add_parameter()

        self._create_status_parameters(
            result_details_tooltip="Spike result, including which open questions it settled",
        )

    # -- preflight ----------------------------------------------------------------------

    def validate_before_workflow_run(self) -> list[Exception] | None:
        """Fail for free, before the run starts and before any credit is spent."""
        errors: list[Exception] = []

        key = GriptapeNodes.SecretsManager().get_secret(self.API_KEY_NAME, should_error_on_not_found=False)
        if not key:
            errors.append(
                ValueError(
                    f"{self.name}: {self.API_KEY_NAME} is not set. Add it under "
                    f"Settings -> API Keys & Secrets, or run: gtn config set-secret {self.API_KEY_NAME} <key>"
                )
            )

        interval = self.get_parameter_value("poll_interval")
        if isinstance(interval, int) and interval < 12:
            errors.append(
                ValueError(
                    f"{self.name}: poll_interval {interval}s is below the 12s floor implied by the "
                    f"5 RPM read cap — raise it to 15."
                )
            )

        return errors or None

    # -- execution ----------------------------------------------------------------------

    async def aprocess(self) -> None:
        self._clear_execution_status()
        # Clear stale outputs so a failure never leaves the previous run's artifact wired downstream.
        self.parameter_output_values["output_video"] = None
        self.parameter_output_values["job_id"] = ""
        self.parameter_output_values["status"] = ""
        self.parameter_output_values["progress"] = 0
        self.parameter_output_values["findings"] = ""
        self.parameter_output_values["raw_job"] = {}

        notes: list[str] = []
        started = time.monotonic()

        try:
            api_key = GriptapeNodes.SecretsManager().get_secret(self.API_KEY_NAME)
            headers = {"x-api-key": api_key}
            prompt = self.get_parameter_value("prompt") or ""
            interval = int(self.get_parameter_value("poll_interval") or POLL_INTERVAL_SECONDS)

            body = {
                "generation_type": "video",
                "source_uri": SAMPLE_SOURCE,
                "reference_image_uri": SAMPLE_REFERENCE,
                "alpha_uri": SAMPLE_ALPHA,
                "alpha_mode": "custom",
                "max_resolution": 720,
                "prompt": prompt,
            }
            # Same scheme the library will use everywhere: same config resumes, changed config
            # submits fresh. A bare prefix would silently return a stale job after a prompt edit.
            # sha1 is a config fingerprint here, not a security primitive.
            config_hash = hashlib.sha1(json.dumps(body, sort_keys=True).encode()).hexdigest()[:8]
            body["idempotency_key"] = f"spike_sample_{config_hash}"

            async with httpx.AsyncClient(base_url=BASE_URL, headers=headers, timeout=60.0) as client:
                # --- account limits: are 5 RPM / 10 concurrent actually our numbers? ---
                try:
                    acct = await client.get("/account/info")
                    if acct.status_code == httpx.codes.OK:
                        limits = acct.json().get("rate_limits") or {}
                        notes.append(f"account rate_limits = {json.dumps(limits)}")
                    else:
                        notes.append(f"/account/info returned {acct.status_code}: {acct.text[:200]}")
                except httpx.HTTPError as e:  # non-fatal, it's diagnostic only
                    notes.append(f"/account/info failed: {e}")

                # --- submit ---
                self.publish_update_to_parameter("status", "submitting")
                resp = await client.post("/switchx/generations", json=body)
                if resp.status_code != httpx.codes.OK:
                    msg = f"Submit failed HTTP {resp.status_code}: {resp.text[:500]}"
                    raise RuntimeError(msg)

                job = resp.json()
                job_id = job.get("id", "")
                self.parameter_output_values["job_id"] = job_id
                self.publish_update_to_parameter("job_id", job_id)
                notes.append(f"submit -> id={job_id} status={job.get('status')} seed={job.get('seed')}")
                notes.append(f"submit response keys = {sorted(job.keys())}")

                # --- poll ---
                job = await self._poll(client, job_id, interval, notes)

                # --- download ---
                render_url = self._pick_render_url(job, notes)
                dl = await client.get(render_url, headers={})  # signed URL: no api key
                dl.raise_for_status()
                render_bytes = dl.content
                notes.append(
                    f"render downloaded: {len(render_bytes) / 1_048_576:.2f} MB, "
                    f"content-type={dl.headers.get('content-type')}"
                )

            # --- save (gotcha 8: saved.location, never dest.location) ---
            dest = self._file_param.build_file()
            saved = dest.write_bytes(render_bytes)
            notes.append(f"saved.location = {saved.location}")
            notes.append(f"audio: {self._audio_note(render_bytes)}")

            self.parameter_output_values["output_video"] = VideoUrlArtifact(saved.location)
            self.parameter_output_values["raw_job"] = job
            self.parameter_output_values["status"] = job.get("status", "")
            self.parameter_output_values["progress"] = 100

            elapsed = time.monotonic() - started
            notes.append(f"wall clock: {elapsed:.0f}s")
            findings = "\n".join(f"- {n}" for n in notes)
            self.parameter_output_values["findings"] = findings
            self._set_status_results(was_successful=True, result_details=f"Spike succeeded.\n{findings}")

        except Exception as e:
            self.parameter_output_values["output_video"] = None
            self.parameter_output_values["findings"] = "\n".join(f"- {n}" for n in notes)
            self._set_status_results(
                was_successful=False,
                result_details=f"{self.name}: spike failed: {e}\n" + "\n".join(f"- {n}" for n in notes),
            )
            self._handle_failure_exception(e)

    async def _poll(self, client: httpx.AsyncClient, job_id: str, interval: int, notes: list[str]) -> dict[str, Any]:
        max_attempts = max(1, (TIMEOUT_MINUTES * 60) // interval)
        saw_progress: list[int] = []

        for _attempt in range(max_attempts):
            if self.is_cancellation_requested():
                msg = "Cancelled by user"
                raise RuntimeError(msg)

            await asyncio.sleep(interval)

            resp = await client.get(f"/switchx/generations/{job_id}")
            if resp.status_code != httpx.codes.OK:
                msg = f"Poll failed HTTP {resp.status_code}: {resp.text[:300]}"
                raise RuntimeError(msg)

            job = resp.json()
            status = job.get("status", "")
            progress = job.get("progress") or 0
            saw_progress.append(progress)

            self.publish_update_to_parameter("status", status)
            self.publish_update_to_parameter("progress", progress)

            if status == "completed":
                notes.append(f"progress sequence observed = {saw_progress}")
                notes.append(f"completed response keys = {sorted(job.keys())}")
                return job
            if status == "failed":
                # `error` is nullable-but-present, so .get("error", default) never fires its default.
                msg = f"Job failed: {job.get('error') or 'unknown'}"
                raise RuntimeError(msg)

        msg = f"Timed out after {TIMEOUT_MINUTES} min. Job {job_id} may still complete — re-fetch by id."
        raise RuntimeError(msg)

    @staticmethod
    def _pick_render_url(job: dict[str, Any], notes: list[str]) -> str:
        """Find the render URL without assuming a field name the docs don't pin down."""
        for key in ("render_url", "output_url", "result_url", "video_url", "url"):
            if job.get(key):
                notes.append(f"render URL field name = {key!r}")
                return str(job[key])

        # Nested shapes: {"output": {...}} / {"outputs": {...}}
        for container in ("output", "outputs", "result"):
            sub = job.get(container)
            if isinstance(sub, dict):
                for key in ("render_url", "url", "video", "render"):
                    if sub.get(key):
                        notes.append(f"render URL field name = {container}.{key}")
                        return str(sub[key])

        msg = f"Could not find a render URL. Response keys: {sorted(job.keys())}. Full: {json.dumps(job)[:800]}"
        raise RuntimeError(msg)

    @staticmethod
    def _audio_note(data: bytes) -> str:
        """Cheap audio check with no ffprobe dependency.

        Looks for an MP4 audio-codec box in the container header. Presence is strong evidence of
        an audio track; absence is suggestive, not proof. ffprobe confirms it properly.
        """
        head = data[:262_144]
        found = [tag.decode() for tag in (b"mp4a", b"esds", b"Audio", b"soun") if tag in head]
        if found:
            return f"likely HAS audio (found {found} in container header) -> Restore Audio may be unnecessary"
        return "likely SILENT (no mp4a/soun box in first 256 KB) -> Restore Audio justified; confirm with ffprobe"
