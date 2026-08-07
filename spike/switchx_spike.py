"""Throwaway spike node: proves the whole Beeble SwitchX loop end to end.

Not part of the library. Its job is to settle the open questions in CLAUDE.md against a live engine
and a live API, then be deleted.

It now drives the real ``beeble_library`` modules rather than reimplementing them, so a green run
also exercises the client's retry/rate-limit path and uri.py's localhost-to-upload path -- the two
riskiest pieces of P0. That requires the manifest to sit at the repo root, because the engine adds
the *manifest's* directory to sys.path.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any

from griptape.artifacts.video_url_artifact import VideoUrlArtifact
from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import SuccessFailureNode
from griptape_nodes.exe_types.param_components.project_file_parameter import ProjectFileParameter
from griptape_nodes.exe_types.param_types.parameter_image import ParameterImage
from griptape_nodes.exe_types.param_types.parameter_video import ParameterVideo
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.traits.options import Options

from beeble_library.client import BeebleClient, describe_exception, job_error, output_urls
from beeble_library.constants import (
    API_KEY_NAME,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_TIMEOUT_MINUTES,
    MIN_POLL_INTERVAL_SECONDS,
)
from beeble_library.errors import BeebleError
from beeble_library.uri import resolve

# Beeble's public quickstart assets. Used when no source is wired in, so the node runs standalone.
SAMPLE_SOURCE = "https://cdn.beeble.ai/public/developer-api/source.mp4"
SAMPLE_REFERENCE = "https://cdn.beeble.ai/public/developer-api/reference.png"
SAMPLE_ALPHA = "https://cdn.beeble.ai/public/developer-api/alpha.mp4"


class SwitchXSpike(SuccessFailureNode):
    """Submit one SwitchX video generation, poll it, download the render."""

    API_KEY_NAME = API_KEY_NAME

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.category = "SwitchX"
        self.description = "Beeble Relighting Tool"

        # --- inputs ---------------------------------------------------------------------
        self.add_parameter(
            ParameterVideo(
                name="source_video",
                tooltip=(
                    "Video to transform. Leave empty to use Beeble's public sample clip. "
                    "Localhost artifacts are uploaded to Beeble automatically."
                ),
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            ParameterImage(
                name="reference_image",
                tooltip="Reference image driving the look. Optional if a prompt is supplied.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="prompt",
                type="str",
                default_value="cinematic golden hour rim light from camera left, warm practical fill",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
                tooltip="Art direction. One of prompt or reference_image is required.",
                ui_options={"multiline": True},
            )
        )
        self.add_parameter(
            Parameter(
                name="alpha_mode",
                type="str",
                default_value="auto",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
                tooltip=(
                    "auto: isolate the main subject, no alpha needed. fill: whole frame, keeps "
                    "geometry. custom/select: needs a matching alpha, only wired up here for the "
                    "sample clip."
                ),
                traits={Options(choices=["auto", "fill", "custom", "select"])},
            )
        )
        self.add_parameter(
            Parameter(
                name="max_resolution",
                type="int",
                default_value=720,
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
                tooltip="Output cap. 720 is the cheap one -- keep it there while spiking.",
                traits={Options(choices=[720, 1080])},
            )
        )
        self.add_parameter(
            Parameter(
                name="poll_interval",
                type="int",
                default_value=DEFAULT_POLL_INTERVAL_SECONDS,
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
                tooltip=f"Seconds between polls. Never below {MIN_POLL_INTERVAL_SECONDS} (5 RPM read cap).",
            )
        )

        # --- outputs --------------------------------------------------------------------
        self.add_parameter(
            ParameterVideo(
                name="output_video",
                tooltip="The downloaded render",
                allowed_modes={ParameterMode.OUTPUT, ParameterMode.PROPERTY},
                settable=False,
                ui_options={"pulse_on_run": True},
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
                tooltip="Full final status response -- ground truth for undocumented fields",
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
                ValueError(f"{self.name}: {self.API_KEY_NAME} is not set. Add it under Settings -> API Keys & Secrets.")
            )

        interval = self.get_parameter_value("poll_interval")
        if isinstance(interval, int) and interval < MIN_POLL_INTERVAL_SECONDS:
            errors.append(
                ValueError(
                    f"{self.name}: poll_interval {interval}s is below the {MIN_POLL_INTERVAL_SECONDS}s "
                    f"floor implied by the 5 RPM read cap -- raise it to {DEFAULT_POLL_INTERVAL_SECONDS}."
                )
            )

        if not self.get_parameter_value("prompt") and not self.get_parameter_value("reference_image"):
            errors.append(
                ValueError(f"{self.name}: supply a prompt or a reference_image (MISSING_STYLE_INPUT otherwise).")
            )

        alpha_mode = self.get_parameter_value("alpha_mode")
        if alpha_mode in ("custom", "select") and self.get_parameter_value("source_video"):
            errors.append(
                ValueError(
                    f"{self.name}: alpha_mode={alpha_mode!r} needs a matching alpha, which this spike only "
                    f"has for the built-in sample clip. Use 'auto' or 'fill' with your own footage."
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
            interval = int(self.get_parameter_value("poll_interval") or DEFAULT_POLL_INTERVAL_SECONDS)

            async with BeebleClient(api_key) as client:
                # Live limits first: 5 RPM / 10 concurrent are defaults, not necessarily ours.
                try:
                    info = await client.sync_rate_limits()
                    notes.append(f"account rate_limits = {json.dumps(info.get('rate_limits'))}")
                except BeebleError as e:  # diagnostic only, never fatal
                    notes.append(f"/account/info failed: {e}")

                body = await self._build_body(client, notes)

                self.publish_update_to_parameter("status", "submitting")
                job = await client.submit_generation(body)
                job_id = job.get("id", "")
                self.parameter_output_values["job_id"] = job_id
                self.publish_update_to_parameter("job_id", job_id)
                notes.append(f"submit -> id={job_id} status={job.get('status')} seed={job.get('seed')}")
                notes.append(f"submit response keys = {sorted(job.keys())}")

                job = await self._poll(client, job_id, interval, notes)

                urls = output_urls(job)
                notes.append(f"output urls present = { {k: bool(v) for k, v in urls.items()} }")
                render_url = urls["render"]
                if not render_url:
                    msg = f"Job completed but output.render was null. Full response: {json.dumps(job)[:600]}"
                    raise RuntimeError(msg)

                render_bytes = await client.download(render_url)
                notes.append(f"render downloaded: {len(render_bytes) / 1_048_576:.2f} MB")

            # Save (gotcha 8: capture the return value; dest.location stays unresolved).
            dest = self._file_param.build_file()
            saved = dest.write_bytes(render_bytes)
            notes.append(f"saved.location = {saved.location}")
            notes.append(f"audio: {self._audio_note(render_bytes)}")

            self.parameter_output_values["output_video"] = VideoUrlArtifact(value=saved.location, name=saved.name)
            self.parameter_output_values["raw_job"] = job
            self.parameter_output_values["status"] = job.get("status", "")
            self.parameter_output_values["progress"] = 100

            notes.append(f"wall clock: {time.monotonic() - started:.0f}s")
            findings = "\n".join(f"- {n}" for n in notes)
            self.parameter_output_values["findings"] = findings
            self._set_status_results(was_successful=True, result_details=f"Spike succeeded.\n{findings}")

        except asyncio.CancelledError:
            # CancelledError subclasses BaseException, so `except Exception` never sees it and the
            # engine reports "Failed with error:" with nothing after it. Report, then re-raise --
            # swallowing a cancellation would leave the engine thinking the node is still running.
            notes.append(f"CANCELLED after {time.monotonic() - started:.0f}s")
            self.parameter_output_values["output_video"] = None
            self.parameter_output_values["findings"] = "\n".join(f"- {n}" for n in notes)
            self._set_status_results(
                was_successful=False,
                result_details=(
                    f"{self.name}: cancelled. The job may still be running on Beeble's side and will "
                    f"still be billed -- re-fetch by job_id rather than resubmitting.\n"
                    + "\n".join(f"- {n}" for n in notes)
                ),
            )
            raise

        except Exception as e:
            detail = describe_exception(e)
            notes.append(f"FAILED after {time.monotonic() - started:.0f}s at the stage above")
            self.parameter_output_values["output_video"] = None
            self.parameter_output_values["findings"] = "\n".join(f"- {n}" for n in notes)
            self._set_status_results(
                was_successful=False,
                result_details=f"{self.name}: spike failed: {detail}\n" + "\n".join(f"- {n}" for n in notes),
            )
            self._handle_failure_exception(e)

    async def _build_body(self, client: BeebleClient, notes: list[str]) -> dict[str, Any]:
        """Assemble the request, resolving any wired-in media to URIs Beeble can fetch."""
        source_video = self.get_parameter_value("source_video")
        reference_image = self.get_parameter_value("reference_image")
        alpha_mode = self.get_parameter_value("alpha_mode") or "auto"
        using_sample = not source_video

        if using_sample:
            source_uri = SAMPLE_SOURCE
            notes.append("source = Beeble sample clip")
        else:
            resolved = await resolve(source_video, client)
            source_uri = resolved.uri
            notes.append(
                f"source resolved from {resolved.scheme} -> {'upload' if resolved.uploaded else 'passthrough'}"
            )

        body: dict[str, Any] = {
            "generation_type": "video",
            "source_uri": source_uri,
            "alpha_mode": alpha_mode,
            "max_resolution": int(self.get_parameter_value("max_resolution") or 720),
        }

        prompt = self.get_parameter_value("prompt")
        if prompt:
            body["prompt"] = prompt

        if reference_image:
            resolved_ref = await resolve(reference_image, client)
            body["reference_image_uri"] = resolved_ref.uri
            notes.append(f"reference resolved from {resolved_ref.scheme}")
        elif using_sample and not prompt:
            body["reference_image_uri"] = SAMPLE_REFERENCE

        if alpha_mode in ("custom", "select"):
            # Preflight already rejects these with user-supplied footage.
            body["alpha_uri"] = SAMPLE_ALPHA

        # Same scheme the library uses everywhere: same config resumes, changed config submits fresh.
        config_hash = hashlib.sha1(json.dumps(body, sort_keys=True).encode()).hexdigest()[:8]
        body["idempotency_key"] = f"spike_{config_hash}"
        return body

    async def _poll(self, client: BeebleClient, job_id: str, interval: int, notes: list[str]) -> dict[str, Any]:
        max_attempts = max(1, (DEFAULT_TIMEOUT_MINUTES * 60) // interval)
        seen_progress: list[int] = []

        for _attempt in range(max_attempts):
            # NOTE: a property, not a method. Calling it raises "'bool' object is not callable".
            if self.is_cancellation_requested:
                msg = "Cancelled by user"
                raise RuntimeError(msg)

            await asyncio.sleep(interval)

            job = await client.get_job(job_id)
            status = job.get("status", "")
            progress = job.get("progress") or 0
            seen_progress.append(progress)

            self.publish_update_to_parameter("status", status)
            self.publish_update_to_parameter("progress", progress)

            if status == "completed":
                notes.append(f"progress sequence observed = {seen_progress}")
                notes.append(f"completed response keys = {sorted(job.keys())}")
                return job
            if status == "failed":
                msg = f"Job failed: {job_error(job)}"
                raise RuntimeError(msg)

        msg = f"Timed out after {DEFAULT_TIMEOUT_MINUTES} min. Job {job_id} may still complete -- re-fetch by id."
        raise RuntimeError(msg)

    @staticmethod
    def _audio_note(data: bytes) -> str:
        """Cheap audio check with no ffprobe dependency.

        Presence of an audio box is strong evidence of a track; absence is suggestive, not proof.
        """
        head = data[:262_144]
        found = [tag.decode() for tag in (b"mp4a", b"esds", b"soun") if tag in head]
        if found:
            return f"likely HAS audio (found {found}) -> Restore Audio may be unnecessary"
        return "likely SILENT (no mp4a/soun box in first 256 KB) -> Restore Audio justified; confirm with ffprobe"
