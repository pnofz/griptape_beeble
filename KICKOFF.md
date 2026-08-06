# Handoff to Claude Code CLI

## Setup

```bash
mkdir -p griptape-nodes-library-beeble/docs && cd griptape-nodes-library-beeble
git init
# copy the two files from your Cowork outputs folder:
#   CLAUDE.md                                  → ./CLAUDE.md
#   griptape-nodes-library-beeble-DESIGN.md    → ./docs/DESIGN.md
claude
```

`CLAUDE.md` loads automatically as project context. Confirm with `/memory`.

---

## Prompt 1 — the spike (do this first)

> Read CLAUDE.md and docs/DESIGN.md.
>
> Before we build the library, write a single throwaway spike node that proves the whole loop end to
> end against the real API. Put it in `spike/switchx_spike.py` with a minimal
> `griptape-nodes-library.json` beside it so I can register it in the engine.
>
> The spike should: read `BEEBLE_API_KEY` via SecretsManager; POST a generation using Beeble's
> public sample assets (source.mp4, reference.png, alpha.mp4, `alpha_mode: "custom"`,
> `max_resolution: 720`, `generation_type: "video"`); poll at 15 s with a progress-bar output and
> cancellation checks; download the render; save it via ProjectFileParameter and emit a
> VideoUrlArtifact.
>
> Then give me a checklist of the open questions in CLAUDE.md that running it will answer — in
> particular the manifest filename, the schema/engine version my install accepts, whether
> `saved.location` behaves as documented, and whether the render comes back with or without audio.
> Note anything the spike can't settle.

## Prompt 2 — P0, after the spike

> The spike worked. Update CLAUDE.md's open-questions section with what we learned, then scaffold
> P0 per docs/DESIGN.md §8: pyproject.toml, the manifest, and the support modules (client.py,
> constants.py, errors.py, uri.py, probe.py, base.py) with tests before any nodes.
>
> client.py is the only module importing httpx and owns process-wide read/write token buckets sized
> from live account limits. errors.py covers all 26 documented error codes. Use httpx MockTransport
> for the client tests — no live calls in the suite.
>
> Stop after the modules and tests pass. We'll do the 15 nodes next.

## Prompt 3 — nodes

> Now the P0 nodes, in this order: Upload → Resolve URI → Alpha Config → Generation Config →
> Submit → Get → Wait → Fetch Output → Validate Source → Validate Request → Inspect Media →
> Fit To Pixel Budget → Trim To Frame Limit → SwitchX Video → SwitchX Image.
>
> One node per commit, registered in the manifest as you go. Follow the Griptape conventions section
> of CLAUDE.md exactly — SuccessFailureNode for network nodes, aprocess with httpx, ParameterVideo /
> ParameterImage for media, and the ten gotchas.

---

## Worth knowing

- **`/init`** would generate its own CLAUDE.md — don't, you already have a better one. Use
  `/memory` to edit it instead.
- Add the Beeble docs as context when a node's params are in question:
  `Read https://api.beeble.ai/developer-api-docs/openapi.json` — it's the authoritative schema, and
  the prose docs at `https://developer.beeble.ai/docs/llms-full.txt` are LLM-friendly by design.
- Griptape docs pages are all available as post-processed markdown by appending `/index.md` to the
  page URL — much better than raw GitHub for node authoring questions.
- The spike **spends real credit**. Small (720p, ~5 s sample clip) but not free.
- Keep `docs/DESIGN.md` as the spec of record. When a design decision changes during
  implementation, update the doc rather than letting code and spec drift.
