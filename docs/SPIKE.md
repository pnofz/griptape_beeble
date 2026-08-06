# Running the spike

The spike is one throwaway node that proves the whole Beeble SwitchX loop end to end: read the API
key, submit a generation, poll it, download the render, save it into the workspace, emit a
`VideoUrlArtifact`. Its only purpose is to settle the open questions in `CLAUDE.md` against a live
engine and a live API, then be deleted.

> **It spends real credit.** 720p, ~5 s sample clip — small, but not free. There is no dry-run mode
> and no cancel endpoint, so once a generation is submitted you are committed to paying for it.

## Status

**Never executed.** It compiles, `ruff check` passes, and all seven engine imports resolve against
`griptape_nodes_engine 0.94.2`. But nothing has run it, so treat every claim about runtime behaviour
below as a hypothesis the spike is designed to test.

## Prerequisites

1. **Engine v0.94.4** — `griptape-nodes --version`.
2. **`BEEBLE_API_KEY` registered** — see [Setting the API key](../README.md#setting-the-api-key). The
   node's `validate_before_workflow_run()` blocks the run in the editor if it's missing, so you get a
   clear error rather than a confusing 401 mid-flight.
3. **Credit on the Beeble account.**

## 1. Register the library

There is no `gtn libraries register` subcommand — `gtn libraries` only has `sync` and `download`.
Registration happens through config or the editor.

**Option A — editor UI.** Settings → *Libraries* → add the path to
`spike/griptape_nodes_library.json`.

**Option B — config file.** Add the manifest path to
`~/.config/griptape_nodes/griptape_nodes_config.json` under
`app_events.on_app_initialization_complete.libraries_to_register`:

```json
"libraries_to_register": [
  "C:\\Users\\pnofz\\griptape_claude\\spike\\griptape_nodes_library.json",
  "...existing entries..."
]
```

Entries may be a bare path string or an object with `path` plus `enabled` /
`worker_mode_override` (`settings.py:119-143`). The path may point at a single manifest **or** a
folder, which is scanned recursively.

Restart the engine. The node appears under **SwitchX/Spike** as *SwitchX Spike*.

## 2. Run it

Drop the node into an empty workflow and run. It needs no inputs — it uses Beeble's public
quickstart assets, which are already `https`-reachable and therefore sidestep the
localhost-artifact problem (`CLAUDE.md` gotcha 6):

```
source_uri          https://cdn.beeble.ai/public/developer-api/source.mp4
reference_image_uri https://cdn.beeble.ai/public/developer-api/reference.png
alpha_uri           https://cdn.beeble.ai/public/developer-api/alpha.mp4
alpha_mode          custom
max_resolution      720
generation_type     video
```

Expect roughly 1–5 minutes of wall clock, polled at 15 s. The `progress` output drives a progress
bar and `status` tracks `in_queue → processing → completed`.

**Leave `poll_interval` at 15.** Preflight rejects anything below 12, because 5 RPM reads means one
request per 12 s.

## 3. Record what it found

The node writes a `findings` output — a bullet list — plus `raw_job`, the complete final status
response. **Copy both into `CLAUDE.md`** and delete the corresponding open questions.

`findings` reports:

| Reports | Settles |
|---|---|
| `account rate_limits = {...}` | Whether 5 RPM / 10 concurrent are actually *your* numbers, or the account is provisioned higher |
| `render URL field name = '...'` | Open question 2 — the docs never name the field. The spike probes a candidate list and reports the winner |
| `submit response keys` / `completed response keys` | The real response shape, including undocumented fields |
| `progress sequence observed = [...]` | Whether `progress` is meaningfully granular or jumps 0 → 100 |
| `saved.location = ...` | Gotcha 8 at runtime — confirmed in source, unconfirmed in a live graph |
| `audio: ...` | Open question 1 — whether `Restore Audio` needs to exist at all |
| `wall clock: Ns` | Whether the 20-minute default timeout is sane |

Also confirm by hand, since they can't be self-reported: that the progress bar actually animates,
and that cancelling mid-poll exits promptly rather than hanging until the next tick.

## What the spike cannot settle

Do not let a green run convince you these are answered:

- **Audio is a heuristic, not a measurement.** With no ffprobe available, the spike scans the first
  256 KB of the container for `mp4a` / `soun` / `esds` boxes. Presence is strong evidence of an audio
  track; absence is suggestive only. Confirm with `ffprobe` before deciding `Restore Audio`'s fate.
- **`workflows[]` authoring format** (open question 6) — needs a workflow, not a node.
- **Temporal continuity across chunked jobs** (4) — needs a >240-frame plate split across two jobs.
- **Colour management** (5) — needs a known-value test chart round-tripped.
- **List-endpoint ordering and URL freshness** (7) — needs several jobs and a second look after the
  72 h expiry.
- **API-key-authenticated pricing** (8) — that's a support email to Beeble, not a test.

## When it fails

The node is a `SuccessFailureNode`, so failures route out the **Failed** branch rather than crashing
the graph — but only if you've connected something to it. With nothing connected, the exception
propagates, which is what you want while spiking.

- **Preflight error about `BEEBLE_API_KEY`** — the key isn't registered. Nothing was spent.
- **`Submit failed HTTP 4xx`** — read the body in `result_details`; it carries Beeble's
  `{error: {message, code}}` envelope. Nothing was spent.
- **`Could not find a render URL`** — the job completed and **you were charged**, but the response
  field name isn't in the candidate list. `findings` and the exception both dump the response keys;
  add the right name and re-run. The `idempotency_key` means a re-run with unchanged config returns
  the *same* job rather than billing twice.
- **Timeout** — `job_id` is preserved on the output specifically so the spend isn't lost. Re-fetch by
  id rather than resubmitting.

## After it passes

1. Update `CLAUDE.md` — move settled items out of *Open questions* into the resolved section.
2. Build P0 per `docs/DESIGN.md` §8: support modules and their tests **before** any nodes.
3. **Delete `spike/`.** It is scaffolding, not a deliverable, and it duplicates logic that belongs in
   `client.py`.
