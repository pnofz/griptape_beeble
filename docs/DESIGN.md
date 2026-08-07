# griptape-nodes-library-beeble — Node Library Design Spec

A Griptape Nodes (Foundry) node library wrapping the **Beeble SwitchX** developer API, designed
for VFX pipeline use rather than one-off browser-style generation.

- **Target repo name:** `griptape-nodes-library-beeble` (matches upstream convention:
  `griptape-nodes-library-kling`, `-googleai`, `-nuke`)
- **API:** `https://api.beeble.ai/v1` — auth via `x-api-key` header
- **Spec sources:** [developer.beeble.ai/docs](https://developer.beeble.ai/docs),
  [OpenAPI](https://api.beeble.ai/developer-api-docs/openapi.json),
  [Griptape custom node docs](https://docs.griptapenodes.com/en/stable/development/custom_nodes/)
- **Status:** design only. No code written yet.

---

## 1. What the API actually gives you

SwitchX is a **six-call API** for pipeline purposes — five paths, six operations. The full spec also
exposes `/v1/api-keys`, `/v1/billing/*` and `/v1/admin/*`, but those authenticate with an
`authorization` bearer header (dashboard auth), **not `x-api-key`**, so they are out of reach for a
node library holding only an API key. That has consequences for cost estimation — see §7.

| Call | Endpoint | Notes |
|---|---|---|
| Create upload URL | `POST /v1/uploads` | body `{filename}` → `{id, upload_url, beeble_uri}`. Presigned **PUT**, expires 1 h. `id` is `upload_…` |
| Start generation | `POST /v1/switchx/generations` | returns `{id: "swx_…", status, seed, …}` immediately |
| Poll job | `GET /v1/switchx/generations/{job_id}` | `in_queue → processing → completed \| failed`, `progress` 0–100 |
| List jobs | `GET /v1/switchx/generations?limit&page_token` | cursor pagination. `limit` `1…100`, default `20` |
| Account info | `GET /v1/account/info` | `spending_limit`, live `rate_limits.rpm`, `rate_limits.concurrency` |
| Billing info | `GET /v1/account/billing` | `prepaid_balance`, `current_period_usage`, per-meter `total_usage` — **no unit price** |

The OpenAPI declares **only `200` responses** for every operation. The error envelope
(`{error: {message, code}}`) exists in prose only, so a generated client will carry no error model —
`errors.py` has to supply it.

### Request body (`CreateSwitchXRequest`)

| Field | Required | Values |
|---|---|---|
| `generation_type` | ✅ | `"image"` \| `"video"` |
| `source_uri` | ✅ | `beeble://uploads/{id}/{file}` \| `https://…` \| `data:{mime};base64,…` (≤50 MB) |
| `alpha_mode` | ✅ | `"auto"` \| `"fill"` \| `"custom"` \| `"select"` |
| `prompt` | ⚠️ | ≤2 000 chars. **One of `prompt` or `reference_image_uri` is required** |
| `reference_image_uri` | ⚠️ | same URI schemes as source |
| `alpha_uri` | conditional | required for `custom` and `select` |
| `alpha_keyframe_index` | optional | `select` + video only, `0 … frame_count-1` |
| `seed` | optional | `0 … 4 294 967 295`. Always echoed back. Same seed = **near-identical, not bit-exact** (documented GPU non-determinism) |
| `max_resolution` | optional | `720` \| `1080` (default `1080`). Typed as bare `integer` — the restriction is server-side only, so a local guard earns its keep |
| `callback_url` | optional | HTTPS webhook |
| `idempotency_key` | optional | 1–256 chars — returns the existing job instead of double-charging |

Note the `data:` 50 MB cap is not stated as pre- or post-base64. Base64 inflates ~33 %, so assume
the pessimistic reading (~37 MB of actual file) in `Resolve URI`'s base64 strategy.

### Hard constraints that must become preflight guards

| Constraint | Error code |
|---|---|
| Source ≤ **2 770 000 total pixels** (w × h) | `SOURCE_TOO_LARGE` |
| Video ≤ **240 frames** | `VIDEO_TOO_MANY_FRAMES` |
| Upload extensions: **`.mp4 .mov .png .jpg .jpeg .webp`** · filename **3–255 chars** | `INVALID_FILENAME` |
| Image: PNG / JPEG / WebP · Video: MP4 / MOV, H.264 or HEVC | `INVALID_URI` (type undeterminable) |
| Source type must match `generation_type` (e.g. no video source for image gen) | `INVALID_FILE_FORMAT` |
| `select` alpha **must be an image** (PNG/JPG grayscale) | `ALPHA_MUST_BE_IMAGE` |
| `custom` alpha type must match source type | `ALPHA_TYPE_MISMATCH` |
| Neither prompt nor reference supplied | `MISSING_STYLE_INPUT` |
| Source/alpha/reference URI must be reachable by Beeble | `SOURCE_UNREACHABLE` |
| **5 RPM** writes, **5 RPM** reads, **10 concurrent** jobs — *defaults, not fixed* | `RATE_LIMIT_EXCEEDED` / `CONCURRENT_LIMIT_EXCEEDED` |
| Spending limit **$5 000**/period by default · balance | `HARD_LIMIT_EXCEEDED` / `INSUFFICIENT_BALANCE` |
| `GET /v1/account/billing` is itself capped at **5 RPM** — never call it in a loop | `RATE_LIMIT_EXCEEDED` |
| Output URLs expire after **72 h** (re-poll by id for fresh signed URLs) | — |
| Billing meters: `api_video_1080p`, `api_video_720p`, `api_image_1080p`, `api_image_720p` | — |
| Public-facing apps must display a **"Powered by SwitchX"** logo *and* text | — |

**Rate limits are per-account defaults, not constants.** `UpdateRateLimitsRequest` permits
`1 … 10 000` RPM and `1 … 100` concurrency, and `RateLimitInfo.limit` is nullable where `null`
means *account default / unlimited*. Read them live from `/v1/account/info` →
`rate_limits.{rpm,concurrency}.limit`. **Never hardcode 5 and 10.**

Extreme aspect ratios may be rejected even under the pixel budget. There is **no cancel
endpoint** — `/v1/switchx/generations/{job_id}` declares `get` only, and the sole `delete` in the
whole spec is on `/v1/api-keys/{key_id}`. Whether an abandoned or failed job still bills is
*undocumented* (`CREDIT_DEDUCTION_FAILED` implies deduction is a discrete step, but its timing
relative to completion is not stated) — assume it does and gate before submit.

`SOURCE_UNREACHABLE` is exactly the failure mode **D6** exists to prevent. `errors.py` needs all
**26** documented codes, not just the ones above.

---

## 2. Design decisions

**D1 — Two hero nodes, not one.** Griptape parameter types are static per port. A single node
that emits either an image or a video would need an `any`-typed output, which kills the inline
preview and downstream type checking. So: `SwitchX Video` and `SwitchX Image`, each hard-setting
`generation_type` and emitting `ParameterVideo` / `ParameterImage` respectively. Shared logic
lives in a `_SwitchXGenerateBase` mixin, not in a node.

**D2 — Hero nodes and primitives coexist.** The hero node does upload → submit → poll → download
in one `aprocess()`. The primitives expose each step so a TD can batch, gate on cost, retry, or
hand the job id to an external farm. Both call the same internal `BeebleClient`.

**D3 — Custom port types, so wiring is self-documenting.** Griptape type strings are arbitrary,
so define:

| Type string | Payload | Emitted by |
|---|---|---|
| `BeebleURI` | `str` — a `beeble://`, `https://` or `data:` URI | `Upload`, `Resolve URI` |
| `SwitchXAlpha` | `{alpha_mode, alpha_uri?, alpha_keyframe_index?}` | `Alpha Config` |
| `SwitchXConfig` | full request body minus `source_uri` / `generation_type` | `Generation Config` |
| `SwitchXJob` | full `SwitchXStatusResponse` dict | `Submit`, `Get Job`, `Wait` |
| `SwitchXJobList` | `list[SwitchXJob]` | `List Jobs`, `Batch Submit` |
| `MediaProbe` | `{width, height, frames, fps, duration, codec, pixel_count}` | `Inspect Media` |

Every config node also accepts `str`/`dict` on those ports so they stay hand-editable.

**D4 — `SuccessFailureNode` for anything that touches the network.** It provides the
Succeeded/Failed control branches plus the `was_successful` / `result_details` status group.
A failed shot in a batch should route, not crash the graph.

**D5 — `async def aprocess()` with `httpx.AsyncClient`, never `requests` + `time.sleep()`.**
Blocking calls stall the engine event loop. Poll loops check
`self.is_cancellation_requested()` every iteration and push progress with
`publish_update_to_parameter("progress", n)` against an `int` param carrying
`ui_options={"progress_bar": True}`.

**D6 — Upstream artifacts are localhost URLs.** An `ImageUrlArtifact` coming out of another node
points at `http://localhost:8124/…`, which Beeble cannot reach. Every URI-producing node must
download the bytes locally and either PUT them to a presigned upload URL (preferred) or inline
them as `data:` base64 (only under 50 MB). This is the single most common integration bug.

**D7 — Money is a first-class gate.** There is no cancel endpoint and no cost field on the
request, so cost has to be enforced *before* submit: `Validate Source` and `Validate Request`
raise from `validate_before_workflow_run()` (blocks the run in the UI before anything spends),
and `Spend Guard` branches on live balance and spending limit.

**D8 — Webhooks are exposed but not consumed.** `callback_url` is a passthrough parameter for
teams with their own listener. In-graph completion is polling. Don't build a listener into a node.
Document the contract users will hit: the endpoint must return 2xx within **10 s**; **5 attempts**
with ~1 s / 5 s / 30 s / 2 min backoff, then marked failed and readable via `webhook.status`
(`pending` \| `delivered` \| `failed`). **No signature or HMAC verification is documented** — say so
explicitly rather than leaving it an implicit security assumption.

> **Unverified engine-side claims.** D1 (static per-port types, `any` output killing preview),
> D5 (blocking calls stalling the event loop) and D6 (the `http://localhost:8124` artifact URL)
> come from the Griptape docs, not from Beeble, and were not verified against a running engine.
> Treat them as strong priors to confirm during the P0 spike.

---

## 3. Node inventory

**46 nodes across 9 categories.** Build-order tier in the last column: **P0** = minimum viable
library (15), **P1** = pipeline-grade (20), **P2** = nice-to-have (11).

### 3.1 `switchx` — Hero nodes

| Node | Class | Key inputs | Outputs | Tier |
|---|---|---|---|---|
| **SwitchX Video** | `SwitchXVideo` | `source_video`, `reference_image`, `prompt`, `alpha_mode`, `alpha`, `alpha_keyframe_index`, `max_resolution`, `seed`, `output_file` | `output_video`, `alpha_out`, `source_out`, `job_id`, `seed_used`, `progress`, `status` | P0 |
| **SwitchX Image** | `SwitchXImage` | same, image-typed | `output_image`, `alpha_out`, `source_out`, `job_id`, `seed_used`, `progress`, `status` | P0 |
| **SwitchX Relight** | `SwitchXRelight` | `source_video`, `reference_image` (required), `prompt` (optional) | as Video | P2 |

`SwitchXRelight` is a preset façade: `alpha_mode` locked to `auto`, reference image mandatory,
prompt hinted toward lighting language. It exists so the common ask ("relight this plate to match
this still") is one node with three ports. If it feels redundant during implementation, ship it as
an example workflow instead.

Both hero nodes expose `alpha_keyframe_index` and the `alpha` port only when `alpha_mode` warrants
it, via `after_value_set()` → `show_parameter_by_name` / `hide_parameter_by_name`.

### 3.2 `switchx/assets` — Getting media into Beeble

| Node | Class | Inputs | Outputs | Tier |
|---|---|---|---|---|
| **Beeble Upload** | `BeebleUpload` | `file` (artifact or path), `filename_override` | `beeble_uri`, `upload_id` | P0 |
| **Resolve URI** | `BeebleResolveURI` | `source` (artifact \| path \| https \| beeble uri), `strategy` (`upload` \| `base64` \| `passthrough`) | `beeble_uri`, `scheme`, `size_mb` | P0 |
| **Beeble URI Input** | `BeebleURIInput` | `uri` (text) | `beeble_uri` | P1 |
| **Refresh Output URLs** | `SwitchXRefreshURLs` | `job_id` | `render_url`, `source_url`, `alpha_url` | P1 |

`Resolve URI` is the node that solves **D6** generically — drop it between any media node and any
SwitchX node and URIs just work. It is also the guard against `SOURCE_UNREACHABLE`.

`Refresh Output URLs` must hit the **by-id** endpoint: the "each call generates fresh signed URLs"
guarantee is documented on `GET /v1/switchx/generations/{job_id}`, not on the list endpoint. There
is no expiry field on the response — 72 h is a constant, not an API value.

### 3.3 `switchx/config` — Request assembly

| Node | Class | Inputs | Outputs | Tier |
|---|---|---|---|---|
| **Alpha Config** | `SwitchXAlphaConfig` | `alpha_mode` (dropdown), `alpha` (image/video), `alpha_keyframe_index` | `alpha_config` | P0 |
| **Generation Config** | `SwitchXGenerationConfig` | `prompt`, `reference_image`, `alpha_config`, `max_resolution`, `seed`, `callback_url`, `idempotency_key` | `config` | P0 |
| **Prompt Builder** | `SwitchXPromptBuilder` | `subject`, `lighting`, `environment`, `camera`, `extra` | `prompt`, `char_count` | P2 |
| **Seed Control** | `SwitchXSeed` | `mode` (`fixed` \| `random` \| `increment`), `seed`, `index` | `seed` | P1 |
| **Resolution Preset** | `SwitchXResolutionPreset` | `preset` (`720` \| `1080`) | `max_resolution`, `meter_id` | P2 |

`Prompt Builder` enforces the 2 000-char cap and warns at 90 % via `ParameterMessage`.
`Seed Control` in `increment` mode makes variant sweeps *repeatable* — but be precise with artists
about what that buys: same seed + same inputs yields **visually consistent, not bit-exact** output
(documented GPU non-determinism). Good enough for A/B and look-dev; **not** a deterministic
re-render, so never rely on it to reproduce an approved plate.

### 3.4 `switchx/jobs` — Decomposed execution

| Node | Class | Inputs | Outputs | Tier |
|---|---|---|---|---|
| **Submit Job** | `SwitchXSubmit` | `source_uri`, `generation_type`, `config` | `job_id`, `job`, `seed_used`, `status` | P0 |
| **Wait For Job** | `SwitchXWaitForJob` | `job_id`, `poll_interval`, `timeout_minutes` | `job`, `status`, `progress`, `error` | P0 |
| **Get Job** | `SwitchXGetJob` | `job_id` | `job`, `status`, `progress` | P0 |
| **Fetch Output** | `SwitchXFetchOutput` | `job`, `download_render`, `download_source`, `download_alpha`, `output_file` | `render`, `source`, `alpha`, `saved_paths` | P0 |
| **Job Info** | `SwitchXJobInfo` | `job` | `id`, `status`, `progress`, `seed`, `generation_type`, `alpha_mode`, `created_at`, `modified_at`, `completed_at`, `error`, `webhook_status` | P1 |
| **List Jobs** | `SwitchXListJobs` | `limit` (clamp 1–100), `page_token` | `jobs`, `next_page_token`, `count` | P1 |
| **Retry Job** | `SwitchXRetryJob` | `job`, `reuse_seed`, `new_idempotency_key` | `job_id`, `job` | P1 |
| **Is Terminal** | `SwitchXIsTerminal` | `job` | `is_terminal`, `succeeded`, `failed` | P2 |

`Wait For Job` is a `SuccessFailureNode` whose Failed branch fires on API failure *and* on
timeout — with the job id preserved on the output so you can pick it up later rather than losing
the spend.

`Retry Job` exists because there is no cancel: the recovery move for a bad look is always
resubmit, and reusing the returned `seed` with a tweaked prompt is the controlled way to iterate
(within the near-identical caveat above).

`modified_at` is on the response and worth surfacing — it's the cheapest stall detector for a job
that sits at `processing` without progressing.

### 3.5 `switchx/preflight` — Guards (build these before the batch nodes)

| Node | Class | Inputs | Outputs | Tier |
|---|---|---|---|---|
| **Validate Source** | `SwitchXValidateSource` | `source`, `generation_type`, `strict` | `is_valid`, `reasons`, `probe`, `pixel_count`, `frame_count` | P0 |
| **Validate Request** | `SwitchXValidateRequest` | `source_uri`, `generation_type`, `config` | `is_valid`, `reasons`, `normalized_config` | P0 |
| **Cost Estimate** | `SwitchXCostEstimate` | `generation_type`, `max_resolution`, `frame_count`, `job_count`, **`rate_card`** (user-supplied unit prices) | `estimated_cost_usd`, `meter_id`, `is_api_sourced` (false) | P1 |
| **Spend Guard** | `SwitchXSpendGuard` | `estimated_cost_usd`, `abort_over_usd` | Succeeded / Failed branch, `balance`, `period_usage`, `headroom` | P1 |
| **Rate Limit Gate** | `SwitchXRateLimitGate` | `wait_for_headroom`, `max_wait_seconds` | `rpm_usage`, `rpm_limit`, `concurrency_usage`, `concurrency_limit`, `proceeded` | P1 |

`Validate Source` and `Validate Request` both implement `validate_before_workflow_run()` so the
error surfaces in the editor **before** the run starts — the whole point is to fail for free.
Their reason strings map 1:1 to Beeble's documented error codes, so a rejection reads
`SOURCE_TOO_LARGE: 3 686 400 px exceeds the 2 770 000 px budget — insert Fit To Pixel Budget`.

**`Cost Estimate` cannot read live pricing with an API key.** `unit_price_cents` appears only on
`GET /v1/billing/usage-summary` (`UsageSummaryResponse.meters[]`), which authenticates with an
`authorization` bearer header rather than `x-api-key`. `GET /v1/account/billing` returns
`{id, label, unit, total_usage}` per meter with **no price field**. So: take unit prices as a
user-supplied rate card input, label the output an estimate, and confirm with Beeble whether an
API-key-authenticated pricing endpoint exists.

`Spend Guard` must handle nulls and both billing types. Every field on `AccountBillingResponse` is
nullable, and `prepaid_balance` is meaningless for **postpaid** accounts — for those, compare
`current_period_usage` against `spending_limit` instead. Likewise `Rate Limit Gate`: a
`RateLimitInfo.limit` of `null` means *default / unlimited*, **not zero**.

Because `/v1/account/billing` is itself capped at 5 RPM, `Cost Estimate` and `Spend Guard` must
fetch once per batch and cache — not once per shot. Five shots polling billing would consume the
entire read budget.

### 3.6 `switchx/prep` — ffmpeg-backed media conditioning

| Node | Class | Inputs | Outputs | Tier |
|---|---|---|---|---|
| **Inspect Media** | `InspectMedia` | `media` | `probe`, `width`, `height`, `frames`, `fps`, `duration`, `codec`, `pixel_count` | P0 |
| **Fit To Pixel Budget** | `FitToPixelBudget` | `media`, `budget_pixels`, `even_dimensions` | `media`, `scale_factor`, `new_width`, `new_height` | P0 |
| **Trim To Frame Limit** | `TrimToFrameLimit` | `media`, `max_frames`, `mode` (`trim` \| `chunk`) | `media`, `chunks`, `chunk_count` | P0 |
| **Split To Chunks** | `SplitToChunks` | `media`, `chunk_frames`, `overlap_frames` | `chunks`, `frame_offsets` | P1 |
| **Encode For API** | `EncodeForAPI` | `media`, `codec` (`h264` \| `hevc`), `strip_audio`, `constant_fps` | `media`, `size_mb` | P1 |
| **Alpha From Matte** | `AlphaFromMatte` | `matte` (seq \| video \| EXR alpha), `invert`, `match_to` | `alpha_video`, `frame_count` | P1 |
| **Keyframe Alpha** | `KeyframeAlpha` | `matte`, `frame_index` | `alpha_image`, `frame_index` | P1 |
| **Restore Audio** | `RestoreAudio` | `render`, `original` | `media` | P1 |
| **Conform Output** | `ConformOutput` | `render`, `reference_plate`, `match_res`, `match_fps`, `add_grain` | `media` | P2 |
| **Extract Reference Frame** | `ExtractReferenceFrame` | `media`, `frame_index` | `image` | P2 |

These are what make the guards actionable: `Validate Source` says no, `Fit To Pixel Budget` and
`Trim To Frame Limit` make it yes.

> **`Restore Audio` rests on an assumption.** Nothing in Beeble's OpenAPI or prose docs mentions
> audio, audio stripping, or re-encode behaviour. That a frame-based v2v model returns a silent
> render is a plausible inference, not documented fact — verify against a real render during the
> P0 spike. If the render carries audio through, this node is unnecessary.

`Conform Output` is the delivery-side counterpart. Note the two ceilings are different things:
`max_resolution` caps the **output** at 1080, while the 2 770 000-pixel budget caps the **source**.
They don't coincide — 2560×1080 (2.76 MP) passes the source budget; 2560×1440 (3.69 MP) fails it.
So a 2K/4K plate needs both a downscale on the way in and an explicit up-conform on the way out.

### 3.7 `switchx/batch` — Shot-list driving

| Node | Class | Inputs | Outputs | Tier |
|---|---|---|---|---|
| **Shot List** | `SwitchXShotList` | `source` (CSV \| JSON \| list), `column_map` | `shots`, `shot_count` | P1 |
| **Batch Submit** | `SwitchXBatchSubmit` | `shots`, `max_concurrent` (default: live account limit), `rpm_limit` (default: live), `idempotency_prefix` | `jobs`, `submitted`, `skipped`, `errors` | P1 |
| **Batch Wait** | `SwitchXBatchWait` | `jobs`, `poll_interval`, `timeout_minutes` | `completed`, `failed`, `progress_summary` | P1 |
| **Batch Collect** | `SwitchXBatchCollect` | `completed`, `naming_template`, `output_dir` | `artifacts`, `report`, `manifest_path` | P1 |
| **Variant Sweep** | `SwitchXVariantSweep` | `source_uri`, `base_config`, `seeds` \| `prompts` \| `references` | `shots` | P2 |
| **Batch Report** | `SwitchXBatchReport` | `completed`, `failed` | `markdown`, `csv`, `total_cost_usd` | P2 |

`Batch Submit` owns the concurrency and RPM math: a token bucket sized from the **live** account
limits (not hardcoded 5/10), with idempotency keys so a re-run of a partially failed batch resumes
instead of double-billing. This is the single highest-value node in the library for a facility, and
the one most likely to be got wrong if hand-rolled.

**Pick one idempotency scheme and state it once.** `f"{prefix}_{shot_id}"` protects against network
retries but means a re-run with *edited prompts* silently returns the **old** jobs — the docs define
behaviour only for a matching key and say nothing about reuse with a different body. Recommended:
`f"{prefix}_{shot_id}_{sha1(config)[:8]}"` — same shot + same config resumes; same shot + changed
config submits fresh. `Retry Job`'s `new_idempotency_key` is then just a manual override of that
hash.

`Batch Wait` polls the **list** endpoint once per cycle for status and progress only (`limit`≤100,
paging as needed), then re-fetches each completed job **by id** before download. Two reasons: the
fresh-signed-URL guarantee is documented on the by-id endpoint, not on list; and list ordering is
unspecified, so "newest first" is an assumption — a batch >100 jobs, or one interleaved with older
jobs, may not appear on page 1 at all.

`Variant Sweep` fans one plate across N seeds or N reference images — look-dev A/B in one node,
then straight into `Display Image Grid` from the standard library for review.

### 3.8 `switchx/account` and `switchx/interop`

| Node | Class | Inputs | Outputs | Tier |
|---|---|---|---|---|
| **Account Info** | `BeebleAccountInfo` | — | `email`, `billing_type`, `spending_limit`, `rpm_usage`, `rpm_limit`, `concurrency_usage`, `concurrency_limit` | P1 |
| **Usage Report** | `BeebleUsageReport` | — | `prepaid_balance`, `period_usage`, `meters` (`total_usage` only), `billing_period_start/end` | P1 |
| **To EXR Sequence** | `SwitchXToEXR` | `render`, `output_dir`, `colorspace` | `exr_paths`, `frame_range` | P2 |
| **To Nuke Read** | `SwitchXToNukeRead` | `render`, `alpha`, `script_path` | `nuke_snippet`, `read_paths` | P2 |
| **Attribution Overlay** | `SwitchXAttribution` | `media`, `position`, `opacity` | `media` | P2 |

`To EXR Sequence` / `To Nuke Read` hand off to the existing
[OpenEXR](https://docs.griptapenodes.com/en/stable/libraries/openexr/) and
[Nuke](https://docs.griptapenodes.com/en/stable/libraries/nuke/) Griptape libraries rather than
reimplementing them — this library's job is to produce correctly-named paths and a snippet.

`Usage Report` deliberately drops `daily_usage`, date-range filtering and cost: those live only on
`GET /v1/billing/usage-summary`, which needs dashboard bearer auth, and it returns
`estimated_cost_cents` (integer cents) rather than dollars.

`Attribution Overlay` composites the official **logo *and* text** from the
[Beeble Brand Kit](https://developer.beeble.ai/assets/Beeble_Brand_Kit.zip) — text-only does not
satisfy the terms, and the permitted alternative wording is "Powered by Beeble". Two things to be
honest about in the node's tooltip: the requirement is a credit *clearly visible in the primary UI
where output is displayed*, so burning a mark into delivered media is a convenience rather than
compliance; and `opacity` must not be used to render the credit less than clearly visible. The
requirement is waived only by an explicit **written** Scale/Enterprise Agreement.

---

## 4. Example workflows to ship

Declared in the manifest's `"workflows"` array.

**W1 — `01_hello_switchx.py`** — Load Video → SwitchX Video (`alpha_mode: auto`, prompt only) →
Display Video. Three nodes, proves the key works.

**W2 — `02_reference_relight.py`** — Load Video + Load Image (reference) → SwitchX Relight →
Restore Audio → Save Video. The most common real ask.

**W3 — `03_custom_matte.py`** — Load Video → Inspect Media → Fit To Pixel Budget → Trim To Frame
Limit; matte branch: Alpha From Matte → Alpha Config (`custom`) → Generation Config → Validate
Request → Submit → Wait → Fetch Output → Conform Output. The full pipeline-grade graph.

**W4 — `04_lookdev_sweep.py`** — one plate, Variant Sweep across 6 seeds at `720` → Batch Submit →
Batch Wait → Batch Collect → Display Image Grid. Cheap look-dev, then re-run the winning seed at
`1080`.

**W5 — `05_shot_batch.py`** — Shot List (CSV) → per-shot Validate Source → Cost Estimate → Spend
Guard → Batch Submit → Batch Wait → Batch Collect → Batch Report. The facility workflow.

---

## 5. Repo layout

```
griptape-nodes-library-beeble/
├── griptape_nodes_library.json      # manifest (see §6)
├── pyproject.toml                   # uv, python ~=3.12
├── uv.lock
├── README.md                         # incl. "Powered by SwitchX" attribution note
├── CHANGELOG.md
├── LICENSE
├── beeble_library/
│   ├── __init__.py
│   ├── client.py                     # BeebleClient — the only place httpx lives
│   ├── constants.py                  # PIXEL_BUDGET, MAX_FRAMES, METERS, ALPHA_MODES
│   ├── errors.py                     # error-code → actionable message map
│   ├── uri.py                        # localhost→bytes, upload vs base64 strategy
│   ├── probe.py                       # ffprobe wrapper → MediaProbe
│   ├── base.py                        # _SwitchXNodeBase (secrets, status, poll loop)
│   ├── hero/                          # switchx_video.py, switchx_image.py, switchx_relight.py
│   ├── assets/                        # upload.py, resolve_uri.py, refresh_urls.py
│   ├── config/                        # alpha_config.py, generation_config.py, prompt_builder.py, seed.py
│   ├── jobs/                          # submit.py, wait.py, get.py, fetch_output.py, list.py, retry.py, info.py
│   ├── preflight/                     # validate_source.py, validate_request.py, cost.py, spend_guard.py, rate_gate.py
│   ├── prep/                          # inspect.py, fit.py, trim.py, chunk.py, encode.py, alpha_from_matte.py, …
│   ├── batch/                         # shot_list.py, submit.py, wait.py, collect.py, sweep.py, report.py
│   ├── account/                       # info.py, usage.py
│   └── interop/                       # to_exr.py, to_nuke.py, attribution.py
├── tests/
│   ├── test_client.py                 # httpx mock transport
│   ├── test_validators.py             # every documented error code
│   └── test_uri.py                    # localhost, https, beeble://, data: paths
└── workflows/                          # W1–W5
```

`client.py` being the only module that imports `httpx` is deliberate: it is the seam for mock
testing and the only place retry/backoff logic lives.

---

## 6. Manifest sketch

```json
{
  "name": "Beeble SwitchX",
  "library_schema_version": "0.7.0",
  "settings": [
    {
      "description": "API key required by Beeble SwitchX nodes",
      "category": "app_events.on_app_initialization_complete",
      "contents": { "secrets_to_register": { "BEEBLE_API_KEY": "" } }
    }
  ],
  "metadata": {
    "author": "Rodeo FX",
    "description": "Beeble SwitchX video-to-video compositing and relighting nodes",
    "library_version": "0.1.0",
    "engine_version": "0.94.4",
    "tags": ["video", "relighting", "compositing", "vfx", "beeble", "switchx"],
    "dependencies": { "pip_dependencies": ["httpx>=0.27"] },
    "declarations": [
      { "type": "lifecycle_stage", "stage": "ALPHA" },
      {
        "type": "model_catalog",
        "providers": {
          "beeble": {
            "display_name": "Beeble",
            "terms_url": "https://developer.beeble.ai/docs/brand-attribution",
            "key_support": "REQUIRES_CUSTOMER_KEY",
            "models": {
              "switchx_video": { "display_name": "SwitchX (video)", "key_support": "REQUIRES_CUSTOMER_KEY" },
              "switchx_image": { "display_name": "SwitchX (image)", "key_support": "REQUIRES_CUSTOMER_KEY" }
            }
          }
        }
      }
    ]
  },
  "categories": [
    { "switchx":           { "title": "SwitchX",            "description": "Generate with SwitchX",        "color": "border-amber-500",  "icon": "Wand2" } },
    { "switchx_assets":    { "title": "SwitchX/Assets",     "description": "Uploads and URIs",              "color": "border-blue-500",   "icon": "Upload" } },
    { "switchx_config":    { "title": "SwitchX/Config",     "description": "Request assembly",              "color": "border-slate-500",  "icon": "Sliders" } },
    { "switchx_jobs":      { "title": "SwitchX/Jobs",       "description": "Submit, poll, fetch",           "color": "border-green-500",  "icon": "Play" } },
    { "switchx_preflight": { "title": "SwitchX/Preflight",  "description": "Validation and cost guards",    "color": "border-red-500",    "icon": "ShieldCheck" } },
    { "switchx_prep":      { "title": "SwitchX/Prep",       "description": "Media conditioning",            "color": "border-purple-500", "icon": "Film" } },
    { "switchx_batch":     { "title": "SwitchX/Batch",      "description": "Shot-list driving",             "color": "border-cyan-500",   "icon": "Layers" } },
    { "switchx_account":   { "title": "SwitchX/Account",    "description": "Limits, usage, billing",        "color": "border-yellow-500", "icon": "Wallet" } },
    { "switchx_interop":   { "title": "SwitchX/Interop",    "description": "EXR, Nuke, delivery",           "color": "border-orange-500", "icon": "Share2" } }
  ],
  "nodes": [
    {
      "class_name": "SwitchXVideo",
      "file_path": "beeble_library/hero/switchx_video.py",
      "metadata": {
        "category": "switchx",
        "display_name": "SwitchX Video",
        "description": "Transform a video with SwitchX — relight, swap background, keep the subject",
        "icon": "video",
        "group": "generate",
        "declarations": [ { "type": "model_usage", "model_ids": ["switchx_video"] } ]
      }
    }
  ],
  "workflows": ["workflows/01_hello_switchx.py"],
  "is_default_library": false
}
```

---

## 7. Implementation notes

**Secrets.** `GriptapeNodes.SecretsManager().get_secret("BEEBLE_API_KEY")`, with
`API_KEY_NAME` as a class constant. `get_config_value()` is deprecated. The
`secrets_to_register` block above is what makes the editor prompt for the key under
Settings → API Keys & Secrets.

**Saving outputs.** Use the project-file system, not the deprecated `StaticFilesManager`:

```python
saved = dest.write_bytes(video_bytes)  # capture the return value
self.parameter_output_values["output_video"] = VideoUrlArtifact(saved.location)
```

Using `dest.location` instead of `saved.location` fails with
`missing required variables: file_extension, file_name_base` — the macro resolves at write time.
Expose the path through `ProjectFileParameter(node=self, name="output_file", default_filename=…)`
so artists can redirect renders.

**Poll loop shape.**

```python
for attempt in range(max_attempts):
    if self.is_cancellation_requested():
        raise RuntimeError("Cancelled by user")
    await asyncio.sleep(poll_interval)
    job = await client.get_job(job_id)
    self.publish_update_to_parameter("status", job["status"])
    self.publish_update_to_parameter("progress", job.get("progress") or 0)
    if job["status"] == "completed":
        return job
    if job["status"] == "failed":
        raise RuntimeError(job.get("error") or "unknown")  # error is nullable-but-present
```

**Poll interval arithmetic — get this right or every graph trips the limiter.** Reads are capped at
5 RPM, i.e. **one request per 12 s**. With *N* concurrent waiters at interval *P*, the read rate is
`60N/P` per minute, so you trip when `P < 12N`:

| Concurrent waits | Minimum safe interval |
|---|---|
| 1 | 12 s |
| 2 | 24 s |
| 4 | 48 s |

Default `poll_interval` **15 s**, `timeout_minutes` 20. Beeble's own quickstart polls at 5 s, which
is 2.4× over the limit from a *single* waiter — **do not copy it**. The token bucket must be
**process-wide** (module-level singleton in `client.py`, not per-`BeebleClient`-instance), because
two hero nodes in one graph each run their own poll loop and must share the budget. `poll_interval`
is a floor, not a guarantee: 429 backoff lives inside `client.get_job()`, so actual spacing may be
wider.

**Retry policy in `client.py`.** `RATE_LIMIT_EXCEEDED` → exponential backoff.
`CONCURRENT_LIMIT_EXCEEDED` → do **not** back off blindly; poll active jobs and wait for a slot.
The docs are explicit that these need different strategies. 402s are never retried blindly:
`INSUFFICIENT_BALANCE` and `HARD_LIMIT_EXCEEDED` surface immediately with a top-up prompt,
`BILLING_NOT_CONFIGURED` points at account setup. `CREDIT_DEDUCTION_FAILED` is documented under
**both 402 and 500** and is server-side — it's the one 402-coded error to retry with backoff. Note
the error envelope is only `{error: {message, code}}`, so any "top-up link" is prose inside
`message` or hardcoded by us.

**Error surfacing.** Map **all 26** documented codes (400/401/402/404/429/500) to an actionable
message naming the node that fixes it — `SOURCE_TOO_LARGE` → "insert Fit To Pixel Budget",
`SOURCE_UNREACHABLE` → "insert Resolve URI". Clear stale outputs at the top of `aprocess()` so a
failure never leaves the previous run's artifact wired downstream. On exception: safe defaults →
`_set_status_results(was_successful=False, …)` → `_handle_failure_exception(e)`.

**Idempotency.** Always send a key, using the single scheme from §3.7
(`{prefix}_{shot_id}_{sha1(config)[:8]}`) everywhere — hero nodes, `Submit`, `Batch Submit` and
`Retry` included. Reuse of a key with a *different* body is undefined in the docs, which is exactly
why the config hash is in there.

> The Griptape-side APIs in this section — `is_cancellation_requested()`,
> `publish_update_to_parameter()`, `validate_before_workflow_run()`, `ProjectFileParameter`,
> `saved.location` vs `dest.location`, the `get_config_value()` and `StaticFilesManager`
> deprecations — come from the Griptape docs and engine source but were **not** run against a live
> engine. Confirm during the P0 spike before building 46 nodes on them.

---

## 8. Build order

**P0 — minimum viable: 4 support modules + 15 nodes.** Modules `client.py`, `uri.py`, `probe.py`,
`base.py`; then Upload, Resolve URI; Alpha Config, Generation Config; Submit, Wait, Get, Fetch
Output; Validate Source, Validate Request; Inspect Media, Fit To Pixel Budget, Trim To Frame Limit;
SwitchX Video, SwitchX Image. Ship with W1–W3. This is a genuinely usable library.

**Spike first, before any of it.** One throwaway node that reads `BEEBLE_API_KEY`, submits the
quickstart sample assets, polls at 15 s, and writes the render into the workspace. It settles the
whole §9 list — manifest filename, schema/engine version, `saved.location`, progress-bar UI,
cancellation, and whether the render carries audio — for a few dollars of credit.

**P1 — pipeline-grade: 20 nodes.** Cost Estimate, Spend Guard, Rate Limit Gate; the batch four
(Shot List, Batch Submit, Batch Wait, Batch Collect); Account Info, Usage Report; Alpha From Matte,
Keyframe Alpha, Encode For API, Restore Audio, Split To Chunks; Job Info, List Jobs, Retry Job,
Refresh Output URLs; Seed Control, Beeble URI Input. Ship W4–W5.

**P2 — polish: 11 nodes.** Relight façade, Prompt Builder, Resolution Preset, Conform Output,
Extract Reference Frame, Is Terminal, Variant Sweep, Batch Report, To EXR, To Nuke Read,
Attribution Overlay.

---

## 9. Open questions before coding

1. ~~**Manifest filename**~~ — **RESOLVED:** `griptape_nodes_library.json` (underscores).
   `library_manager.py:361` defines `LIBRARY_CONFIG_FILENAME` as the underscored form and all three
   official libraries use it; the hyphenated form is only a fallback glob in `git_utils.py`. One file
   is sufficient — registration is by explicit path or recursive scan, not by filename convention.
2. ~~**`library_schema_version` / `engine_version`**~~ — **RESOLVED:** schema `0.7.0` (observed range
   on disk is `0.4.0`–`0.7.0`; the `0.10.0` in §6 above was higher than anything real), engine
   `0.94.4` (the installed version). §6 also switched `secrets_to_register` to the name→default
   **object** form for consistency with the official libraries — `settings.py:197` types the field
   `list[str] | dict[str, str]`, so the original array form was valid as well.
3. **ffmpeg dependency** — the whole `prep` category needs ffmpeg/ffprobe. Bundle
   `imageio-ffmpeg` as a pip dependency, or require a system install and validate its presence in
   `validate_before_workflow_run()`? Bundling is friendlier; a facility may prefer the vetted
   system build.
4. **Chunking >240 frames** — *inferred, not documented:* SwitchX likely has no temporal continuity
   across separate jobs, so a 400-frame plate split into two jobs would drift at the seam. Test that
   premise first, then decide: chunk-and-stitch with an overlap blend, or `Trim To Frame Limit`
   hard-fails long plates?
5. **`workflows[]` authoring format** — the manifest key is documented but the expected `.py`
   contents are not; they are retained-mode scripts. Needs one spike against a live engine.
6. **`VideoArtifact` vs `VideoUrlArtifact`** — only `VideoUrlArtifact` is confirmed in
   `griptape.artifacts`. Fine for our outputs, but worth confirming what `Load Video` emits so
   input converters accept both.
7. **Colour management** — *inferred, not documented:* SwitchX appears to be an 8-bit sRGB pipeline.
   Confirm, then decide where the OCIO round-trip happens: inside `Encode For API` /
   `Conform Output`, or delegated to the OpenColorIO library?
8. **Audio behaviour** — does the render carry the source audio, or come back silent? Determines
   whether `Restore Audio` is needed at all. Answered by the spike.
9. **API-key-authenticated pricing** — is there any endpoint that returns unit prices to an
   `x-api-key` caller, or is a user-maintained rate card the only option? Ask Beeble support.
10. **List-endpoint ordering and URL freshness** — is `GET /v1/switchx/generations` newest-first,
    and does it mint fresh signed URLs? Both undocumented, both affect `Batch Wait`'s design.
