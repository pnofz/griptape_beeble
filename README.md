# griptape_beeble

A [Griptape Nodes](https://docs.griptapenodes.com) node library wrapping the
**[Beeble SwitchX](https://developer.beeble.ai/docs)** API — video-to-video relighting and
background replacement — designed for VFX pipeline use rather than one-off browser generation.

> **Status: pre-alpha. No library code exists yet.**
> The design is complete and fact-checked, and a throwaway spike is written but **has never been
> run**. See [Current status](#current-status) before assuming anything works.

---

## Current status

| Piece | State |
|---|---|
| `docs/DESIGN.md` — full spec, 46 nodes across 9 categories | Complete, fact-checked against the OpenAPI schema |
| `CLAUDE.md` — operational contract, gotchas, conventions | Complete; engine facts verified against v0.94.4 |
| `spike/` — throwaway end-to-end probe | **Written, never executed.** Blocked on `BEEBLE_API_KEY` |
| `beeble_library/` — the actual library | Not started |

Nothing here has produced a render yet. The spike is the gate: it proves the engine APIs behave as
the design assumes before 46 nodes get built on them. See [docs/SPIKE.md](docs/SPIKE.md).

## Requirements

- **Griptape Nodes engine** — developed against **v0.94.4**. Check with `griptape-nodes --version`.
- **Python `~=3.12`** and [`uv`](https://docs.astral.sh/uv/). Note the engine's own venvs ship 3.12;
  a system Python 3.10 is not sufficient for this library.
- **A Beeble API key** with credit on the account. Generation **costs real money** — there is no
  free tier and no dry-run mode.
- **ffmpeg / ffprobe** on `PATH` — required by the `switchx/prep` node category (media probing,
  rescaling, trimming). Whether this ends up bundled as `imageio-ffmpeg` or required as a system
  install is still open; see `CLAUDE.md` open question 3.

## Setting the API key

The nodes read `BEEBLE_API_KEY` via the engine's `SecretsManager`. There is **no CLI subcommand**
for this — `gtn config` only exposes `show`, `list` and `reset`. Two working options:

1. **Editor UI** — Settings → *API Keys & Secrets* → set `BEEBLE_API_KEY`.
2. **Directly in the engine's env file** — add a line to
   `~/.config/griptape_nodes/.env` (Windows: `%USERPROFILE%\.config\griptape_nodes\.env`):

   ```
   BEEBLE_API_KEY=your-key-here
   ```

The manifest's `settings[].contents.secrets_to_register` block is what makes the key appear in the
editor's secrets panel in the first place.

## Repo layout

```
griptape_beeble/
├── CLAUDE.md              # operational contract: hard API facts, ten gotchas, conventions
├── KICKOFF.md             # the original build sequence (spike -> P0 modules -> nodes)
├── docs/
│   ├── DESIGN.md          # spec of record: 46 nodes, per-node params, example workflows
│   └── SPIKE.md           # how to run the spike and what to record
└── spike/
    ├── switchx_spike.py            # throwaway end-to-end probe (delete after P0)
    └── griptape_nodes_library.json # minimal manifest so the engine can load it
```

`docs/DESIGN.md` is the **spec of record**. When an implementation decision changes, update the doc
rather than letting code and spec drift.

### A note on naming

This repo is `griptape_beeble`, but `docs/DESIGN.md` §1 and `CLAUDE.md` still refer to
`griptape-nodes-library-beeble`. That isn't an oversight: the design deliberately picked the longer
name to match the upstream convention (`griptape-nodes-library-kling`, `-googleai`, `-nuke`), and
that convention still applies if this is ever published as a public Griptape library. Treat
`griptape_beeble` as the working repo name and the hyphenated form as the eventual published name —
or rename one of them to settle it.

## Which document to read

- **Adding a node?** `docs/DESIGN.md` for the inventory and per-node parameters, then the *Griptape
  conventions* section of `CLAUDE.md` for the base classes and the ten gotchas.
- **Touching the API?** The *Hard API facts* table in `CLAUDE.md`. Don't re-derive it — every row was
  checked against the OpenAPI schema.
- **Running the spike?** [docs/SPIKE.md](docs/SPIKE.md).

## The API in one table

Six operations matter. Base URL `https://api.beeble.ai/v1`, auth via an **`x-api-key`** header.

| Call | Endpoint |
|---|---|
| Create upload URL | `POST /v1/uploads` |
| Start generation | `POST /v1/switchx/generations` |
| Poll job | `GET /v1/switchx/generations/{job_id}` |
| List jobs | `GET /v1/switchx/generations?limit&page_token` |
| Account info (live rate limits) | `GET /v1/account/info` |
| Billing info (no unit prices) | `GET /v1/account/billing` |

There is **no cancel endpoint**. `/v1/api-keys`, `/v1/billing/*` and `/v1/admin/*` require dashboard
bearer auth, not an API key, so they are unreachable from a node library.

### Limits worth memorising

| Constraint | Value |
|---|---|
| Source pixel budget | ≤ 2,770,000 px (w × h) |
| Video length | ≤ 240 frames |
| Rate limits | 5 RPM reads, 5 RPM writes, 10 concurrent — **per-account defaults, read them live** |
| Poll interval floor | **15 s.** 5 RPM reads = one request per 12 s |
| Output URL lifetime | 72 h, re-fetch by job id |

Beeble's own quickstart polls at 5 s. That is 2.4× over the read limit from a *single* waiter — do
not copy it.

## Roadmap

Build tiers are defined in `docs/DESIGN.md` §8.

- **Spike** — one throwaway node, end to end. *Current stage.*
- **P0** (15 nodes + 6 support modules) — a genuinely usable library: upload, URI resolution, config
  assembly, submit/poll/fetch, preflight guards, media conditioning, and the two hero nodes.
- **P1** (20 nodes) — pipeline-grade: batch/shot-list driving, cost and spend guards, rate-limit
  gating, account reporting.
- **P2** (11 nodes) — polish: prompt builder, variant sweep, EXR/Nuke interop, attribution overlay.

## Attribution requirement

Beeble's [brand attribution terms](https://developer.beeble.ai/docs/brand-attribution) require
public-facing applications to display a **logo *and* text** credit — "Powered by SwitchX" (or
"Powered by Beeble") — clearly visible in the primary UI where output is shown. Text alone does not
satisfy the terms. The requirement is waived only by an explicit **written** Scale or Enterprise
agreement.

Two honest caveats: burning a mark into delivered media is a convenience, not compliance, because
the requirement is about the UI where output is displayed; and opacity must not be used to render
the credit less than clearly visible.

## References

- Beeble docs: <https://developer.beeble.ai/docs> · LLM-friendly: `/docs/llms-full.txt`
- Beeble OpenAPI (authoritative): <https://api.beeble.ai/developer-api-docs/openapi.json>
- Griptape node authoring: <https://docs.griptapenodes.com/en/stable/development/custom_nodes/>
  (append `/index.md` to any page URL for post-processed markdown)

## Licence

Not yet chosen. `docs/DESIGN.md` §5 expects a `LICENSE` file; add one before this leaves Rodeo.
