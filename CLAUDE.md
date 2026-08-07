# CLAUDE.md — griptape-nodes-library-beeble

A Griptape Nodes (Foundry) node library wrapping the **Beeble SwitchX** API for VFX pipeline use.

Full design spec: **`docs/DESIGN.md`** — 46 nodes across 9 categories, per-node params, example
workflows, repo layout. Read it before adding nodes. This file is the operational contract:
constraints, gotchas, conventions, and current state.

---

## Hard API facts — do not re-derive these

Base URL `https://api.beeble.ai/v1`. Auth: **`x-api-key`** header. Six relevant operations:

```
POST /v1/uploads                          → {id, upload_url, beeble_uri}   presigned PUT, 1 h expiry
POST /v1/switchx/generations              → {id: "swx_…", status, seed, …}  returns immediately
GET  /v1/switchx/generations/{job_id}     → status; mints FRESH signed URLs each call
GET  /v1/switchx/generations?limit&page_token   limit 1–100, default 20
GET  /v1/account/info                     → spending_limit, live rate_limits{rpm,concurrency}
GET  /v1/account/billing                  → prepaid_balance, current_period_usage, meters (NO price)
```

`/v1/api-keys`, `/v1/billing/*` and `/v1/admin/*` exist but take an `authorization` bearer header
(dashboard auth), **not `x-api-key`** — unreachable from this library.

| Constraint | Value | Error code |
|---|---|---|
| Source pixel budget | ≤ **2 770 000** px (w × h) | `SOURCE_TOO_LARGE` |
| Video length | ≤ **240 frames** | `VIDEO_TOO_MANY_FRAMES` |
| Upload extensions | `.mp4 .mov .png .jpg .jpeg .webp`, filename 3–255 chars | `INVALID_FILENAME` |
| Formats | image PNG/JPEG/WebP · video MP4/MOV, H.264 or HEVC | `INVALID_URI` |
| Source type vs `generation_type` mismatch | — | `INVALID_FILE_FORMAT` |
| `alpha_mode` | `auto` \| `fill` \| `custom` \| `select` | `INVALID_ALPHA_MODE` |
| `select` alpha | must be an **image** (PNG/JPG grayscale) | `ALPHA_MUST_BE_IMAGE` |
| `custom` alpha | type must match source | `ALPHA_TYPE_MISMATCH` |
| Style input | one of `prompt` (≤2000) or `reference_image_uri` required | `MISSING_STYLE_INPUT` |
| `max_resolution` | `720` \| `1080`, default `1080` (no enum in schema — guard locally) | `INVALID_MAX_RESOLUTION` |
| `seed` | `0 … 4 294 967 295` | — |
| URI schemes | `beeble://` \| `https://` \| `data:` (≤50 MB, assume post-base64) | `INVALID_URI` |
| Reachability | Beeble must be able to fetch the URI | `SOURCE_UNREACHABLE` |
| Rate limits | **5 RPM writes, 5 RPM reads, 10 concurrent** — *defaults only* | `RATE_LIMIT_EXCEEDED` / `CONCURRENT_LIMIT_EXCEEDED` |
| Spending limit | $5 000/period default | `HARD_LIMIT_EXCEEDED` |
| Output URLs | expire after **72 h**, re-fetch by id | — |
| Billing meters | `api_video_1080p` `api_video_720p` `api_image_1080p` `api_image_720p` | — |

Job status values: **`in_queue` → `processing` → `completed` \| `failed`**.
Webhook status values: `pending` \| `delivered` \| `failed`.
There is **no cancel endpoint** — `{job_id}` is GET-only.

---

## Ten gotchas that will bite

These were each verified against the OpenAPI spec. Getting any of them wrong costs money or breaks
at runtime.

1. **Poll interval: 5 RPM reads = one request per 12 s.** With *N* concurrent waiters at interval
   *P*, you trip when `P < 12N`. Default to **15 s**, never below. Beeble's own quickstart uses
   `sleep(5)` — that is 2.4× over the limit from a single waiter. **Do not copy it.**
2. **The rate-limit token bucket must be process-wide** — a module-level singleton in `client.py`,
   not per-`BeebleClient`-instance. Two nodes in one graph each run their own poll loop and must
   share the budget. Separate buckets for reads and writes.
3. **Never hardcode 5 RPM / 10 concurrent.** They are per-account defaults; accounts go to 10 000
   RPM and 100 concurrent. Read live from `/v1/account/info`; `rate_limits.*.limit == null` means
   *default/unlimited*, **not zero**.
4. **Live unit pricing is unreachable.** `unit_price_cents` is only on `/v1/billing/usage-summary`
   (bearer auth). `/v1/account/billing` meters carry `{id, label, unit, total_usage}` and **no
   price**. `Cost Estimate` takes a user-supplied rate card and must label its output an estimate.
5. **`/v1/account/billing` is itself capped at 5 RPM.** Fetch once per batch and cache. Never per
   shot — five shots would consume the whole read budget.
6. **Upstream artifact URLs are localhost** (`http://localhost:8124/…`) and Beeble cannot reach
   them. Every URI-producing path must download bytes and PUT them to a presigned upload URL
   (preferred) or inline as `data:` base64. This is what `uri.py` and the `Resolve URI` node exist
   for, and the direct cause of `SOURCE_UNREACHABLE`.
7. **`error` is nullable-but-present** on the status response, so `job.get("error", "unknown")`
   never fires its default. Use `job.get("error") or "unknown"`.
8. **`saved = dest.write_bytes(...)`, then use `saved.location`.** Using `dest.location` fails with
   `missing required variables: file_extension, file_name_base` — the path macro resolves at write
   time.
9. **Always send an `idempotency_key`**, one scheme everywhere:
   `f"{prefix}_{shot_id}_{sha1(config)[:8]}"`. Same shot + same config resumes; changed config
   submits fresh. A bare `{prefix}_{shot_id}` would silently return stale jobs after a prompt edit.
   Reuse of a key with a different body is **undefined** in the docs — hence the hash.
10. **`Batch Wait` polls the list endpoint for status only, then re-fetches by id before download.**
    The fresh-signed-URL guarantee is documented on the by-id endpoint, not on list, and list
    ordering is unspecified.

Two more, lower stakes: `max_resolution` caps **output** at 1080 while the pixel budget caps
**source** — different ceilings (2560×1080 passes, 2560×1440 fails). And same seed gives
**near-identical, not bit-exact** output (documented GPU non-determinism) — fine for A/B, never for
reproducing an approved plate.

---

## Griptape conventions

- **Base class:** `SuccessFailureNode` for anything touching the network (gives Succeeded/Failed
  control branches + the `was_successful` / `result_details` status group). `DataNode` for pure
  transforms. A failed shot in a batch should route, not crash the graph.
- **Async:** `async def aprocess(self) -> None` with `httpx.AsyncClient`. Never `requests` or
  `time.sleep()` — they stall the engine event loop. Wrap unavoidable blocking calls in
  `await asyncio.to_thread(...)`.
- **Cancellation:** check `self.is_cancellation_requested` at the top of every poll iteration. It is
  a **`@property`** (`node_types.py:368`), *not* a method — `self.is_cancellation_requested()` raises
  `'bool' object is not callable`. The standard library's `agent.py:870` confirms the no-parens form.
  The other `BaseNode` properties are `parameters`, `state` and `parent_group`; everything else this
  library calls is a real method.
- **Progress:** `self.publish_update_to_parameter("progress", n)` against an `int` output param
  with `ui_options={"progress_bar": True}`; `"status"` as a `str` output alongside it.
- **Secrets:** `GriptapeNodes.SecretsManager().get_secret("BEEBLE_API_KEY")`, with `API_KEY_NAME`
  as a class constant. Import `GriptapeNodes` at module level. `get_config_value()` is deprecated.
- **Media params:** always `ParameterVideo` / `ParameterImage`, never a generic `Parameter` — you
  lose the inline preview otherwise. Accept `dict` on input (serialized artifacts arrive as dicts).
- **Saving:** `ProjectFileParameter(node=self, name="output_file", default_filename=...)` →
  `.build_file().write_bytes(bytes)`. `StaticFilesManager` is the deprecated path.
- **Validation:** `validate_before_workflow_run()` for anything that should block *before* the run
  starts (this is how preflight guards fail for free); `validate_before_node_run()` otherwise.
  Return `list[Exception] | None`, messages prefixed `f"{self.name}: "` and naming the fix.
- **Dynamic UI:** `after_value_set()` + `show_parameter_by_name` / `hide_parameter_by_name`; clear
  the value when hiding.
- **Container gotcha:** `parent_container_name` (ParameterList ownership) vs `parent_element_name`
  (ParameterGroup nesting) — mixing them up makes params silently vanish on save/reload.
- **On exception:** clear stale outputs → `_set_status_results(was_successful=False, …)` →
  `_handle_failure_exception(e)`. Always `raise ... from e`.

## Custom port types

`BeebleURI` (str) · `SwitchXAlpha` · `SwitchXConfig` · `SwitchXJob` · `SwitchXJobList` ·
`MediaProbe`. Config nodes must also accept `str`/`dict` on those ports so graphs stay
hand-editable.

---

## Repo conventions

- Python **~=3.12**, `uv` for deps, `ruff` line-length 120, `pyright` clean.
- **Only `client.py` imports `httpx`.** It is the seam for mock-transport tests and the single home
  of retry/backoff and the token buckets.
- Declare runtime deps in **both** `pyproject.toml` (dev env) and the manifest's
  `metadata.dependencies.pip_dependencies` (engine installs from the manifest, not pyproject).
- `errors.py` maps **all 26** documented error codes to actionable messages naming the node that
  fixes it (`SOURCE_TOO_LARGE` → "insert Fit To Pixel Budget").
- Retry policy: `RATE_LIMIT_EXCEEDED` → exponential backoff. `CONCURRENT_LIMIT_EXCEEDED` → do
  **not** back off blindly, poll active jobs for a slot. `INSUFFICIENT_BALANCE` /
  `HARD_LIMIT_EXCEEDED` / `BILLING_NOT_CONFIGURED` → never retry. `CREDIT_DEDUCTION_FAILED` is
  documented under both 402 and 500 and is server-side → retry with backoff.
- Tests: `httpx` MockTransport for the client, one case per error code, and `test_uri.py` covering
  localhost / https / `beeble://` / `data:` paths.

```bash
uv sync --all-groups --all-extras
uv run pytest
uv run ruff check --fix && uv run ruff format
uv run pyright
```

---

## Current state

Design spec complete and fact-checked. **Spike written, not yet run** — `spike/switchx_spike.py`
plus `spike/griptape_nodes_library.json`. It compiles and every engine import resolves against
0.94.2, but it has never been executed: `BEEBLE_API_KEY` is not registered on this machine, so
running it is blocked on adding the key.

**P0 support modules landed and green** — `constants.py`, `errors.py`, `client.py`, `uri.py`, plus
`pyproject.toml` and the root manifest. 126 tests pass (httpx MockTransport, no live calls), ruff
clean, pyright clean. These depend only on settled API facts, not on spike results, which is why
they were safe to build first.

**Still missing from P0:** `probe.py` (blocked on the ffmpeg decision, open question 2 — and ffmpeg
is not installed here), `base.py`, and all 15 nodes. `base.py` and the nodes are the parts that
genuinely need the spike, because they depend on *runtime* engine behaviour rather than on APIs
merely existing.

**Do the spike before building anything.** One throwaway node that reads `BEEBLE_API_KEY`, submits
the quickstart sample assets (`https://cdn.beeble.ai/public/developer-api/source.mp4`,
`reference.png`, `alpha.mp4`, `alpha_mode: "custom"`, `max_resolution: 720`), polls at 15 s, and
writes the render into the workspace. For a few dollars of credit it settles every open question
below.

Then **P0**: modules `client.py`, `uri.py`, `probe.py`, `base.py`, `errors.py`, `constants.py`;
nodes Upload, Resolve URI, Alpha Config, Generation Config, Submit, Wait, Get, Fetch Output,
Validate Source, Validate Request, Inspect Media, Fit To Pixel Budget, Trim To Frame Limit,
SwitchX Video, SwitchX Image. Then P1 (20 nodes) and P2 (11) per `docs/DESIGN.md` §8.

## Resolved by static inspection of the installed engine — do not re-derive

Engine installed: **v0.94.4** (`griptape-nodes --version`), package `griptape_nodes_engine 0.94.2`
at `~/.nuke/griptape/sam3/.venv/Lib/site-packages/griptape_nodes`. Reference libraries on disk:
`~/GriptapeNodes/libraries/griptape-nodes-library-{standard,advanced-media,griptape-cloud}` and
`~/GriptapeNodes/griptape-nodes-library-nuke`.

1. **Manifest filename → `griptape_nodes_library.json` (underscores).** `library_manager.py:361`
   sets `LIBRARY_CONFIG_FILENAME = "griptape_nodes_library.json"`; all three official libraries use
   it. The hyphenated form appears only as a fallback glob in `git_utils.py:1211-1216` (and the nuke
   library uses it). Registration is by explicit path (`libraries_to_register`) or recursive
   directory scan, so **one file is enough** — don't ship both.
2. **Versions.** `library_schema_version` in the wild: advanced-media `0.4.0`, standard `0.6.0`,
   nuke `0.7.0` — the engine tolerates a range, so **use `0.7.0`**. `engine_version` → **`0.94.4`**.
   The design's `0.10.0` schema version was higher than anything real; don't use it.
3. **`secrets_to_register` accepts either shape.** `settings.py:197` types it
   `list[str] | dict[str, str]` — an array of names, or a name→default mapping. All three official
   libraries use the **dict** form (`{"BEEBLE_API_KEY": ""}`), so prefer it for consistency, but
   DESIGN §6's original array form was valid too.
4. **`saved.location` vs `dest.location` — gotcha 8 confirmed in source.**
   `FileDestination.write_bytes()` (`files/file.py:824-843`) returns a *new* `File(str(path))` with
   the resolved path, while `FileDestination.location` (`:817`) delegates to the unresolved
   MacroPath. So capturing the return value is mandatory.
5. **Every engine API the design leans on exists** and imports clean under 0.94.2:
   `SuccessFailureNode` (`node_types.py:1812`), `is_cancellation_requested` (`:369`),
   `publish_update_to_parameter` (`:1307`), `_set_status_results` (`:1913`),
   `_handle_failure_exception` (`:1926`), `_create_status_parameters` (`:1873`),
   `ProjectFileParameter` + `build_file()`, `ParameterVideo`, `ParameterImage`,
   `SecretsManager.get_secret(name, *, should_error_on_not_found=...)`, `VideoUrlArtifact`
   (`griptape.artifacts.video_url_artifact`). `type="json"` is an established param type (34 uses in
   the standard library).
6. **ffmpeg is NOT on PATH** on this machine — so the `prep` category cannot work as a system
   dependency today. Leans the Q3 decision toward bundling `imageio-ffmpeg`.
7. **Output URLs live under `output`, not at the top level.** The completed-job response carries a
   `SwitchXOutputUrls` object: `output.render` (composited output), `output.source` (preprocessed
   source), `output.alpha` (alpha matte) — each `string | null`. Confirmed against the OpenAPI
   schema, so the render-URL field name never needed a paid spike run.
8. **The full error-code list is 27 entries / 26 unique.** `CREDIT_DEDUCTION_FAILED` is listed under
   both 402 and 500, which is why the count reads 26. Enumerated verbatim in `beeble_library/errors.py`;
   that module is now the single source of truth. `INVALID_CALLBACK_URL`, `MISSING_SOURCE`,
   `MISSING_ALPHA`, `INVALID_GENERATION_TYPE`, `JOB_NOT_FOUND`, `INVALID_API_KEY`, `INTERNAL_ERROR`,
   `UPLOAD_URL_FAILED` and `JOB_QUEUE_FAILED` were not named anywhere in this file before.

## Open questions — resolve by spike, don't guess

1. **Does the render carry audio,** or come back silent? Decides whether `Restore Audio` exists at
   all. Undocumented — pure inference right now.
2. **ffmpeg:** bundle `imageio-ffmpeg`, or require a system install and check for it in
   `validate_before_workflow_run()`? The whole `prep` category depends on it. (Not installed here —
   see resolved item 6 above.)
3. **Temporal continuity across chunked jobs** — assumed absent (so a 400-frame plate split in two
   would drift at the seam), but untested. Determines whether chunk-and-stitch is viable.
4. **Colour management:** assumed 8-bit sRGB. Confirm, then decide whether the OCIO round-trip lives
   in `Encode For API` / `Conform Output` or is delegated to the OpenColorIO library.
5. **`workflows[]` authoring format:** the manifest key is documented, the expected `.py` contents
   are not (they're retained-mode scripts). One spike against a live engine.
6. **List-endpoint ordering and URL freshness** — both undocumented, both affect `Batch Wait`.
7. **API-key-authenticated pricing** — ask Beeble support whether any endpoint returns unit prices
   to an `x-api-key` caller.

The engine-side APIs are confirmed to **exist and import** (see resolved item 5) but have not been
**executed** in a running graph. The spike is what proves the runtime behaviour — parameter wiring,
progress-bar updates, cancellation, and the `saved.location` write path.

---

## Reference

- Beeble docs: https://developer.beeble.ai/docs · LLM-friendly: `/docs/llms-full.txt`
- OpenAPI (authoritative): https://api.beeble.ai/developer-api-docs/openapi.json
- Griptape node authoring: https://docs.griptapenodes.com/en/stable/development/custom_nodes/
  (every page also available as `…/index.md` post-processed markdown)
- Brand attribution: https://developer.beeble.ai/docs/brand-attribution — public-facing apps need a
  **logo *and* text** "Powered by SwitchX" credit, clearly visible in the primary UI, waived only by
  a written Scale/Enterprise agreement.
