# PMWeb T&M Review Pipeline

**Status: Phase 0 (access reality check) — no pipeline code yet, by design.**

## What this is

A pipeline that finds T&M (Time & Materials) PCO packages in the PMWeb Change
Event module for three Massport projects (L1697-C4, L1820, L1821), runs each
one through the T&M Review Agent already configured in Gryps Agent Builder,
and collects the per-check results into a report, plus a separate ledger of
extracted tag/slip numbers for a future duplicate-detection workflow.

The full project brief — goals, domain context, out-of-scope list, working
protocol — is in [docs/BRIEF.md](docs/BRIEF.md). Read that first.

## Where things stand

- Phase 0 findings (what access we actually have, verified with evidence) are
  in [docs/PHASE0-FINDINGS.md](docs/PHASE0-FINDINGS.md). Headline: the PMWeb
  Change Event records for all three projects ARE queryable in the Gryps AWS
  environment via Athena (AWS's SQL query service). What is NOT yet confirmed:
  whether the attached PCO documents are reachable by Agent Builder, the batch
  CSV format, and how to identify which change events are T&M.
- Proposed output formats (results report, tag ledger) are in
  [docs/FORMATS.md](docs/FORMATS.md).
- The runbook ([RUNBOOK.md](RUNBOOK.md)) is a skeleton; it gets filled in as
  each pipeline stage is built and approved.

## What each file is

| File | What it is |
|---|---|
| `README.md` | This file — orientation. |
| `RUNBOOK.md` | Step-by-step "how to run it" guide (skeleton until stages exist). |
| `docs/BRIEF.md` | The full project brief from the project owner. |
| `docs/PHASE0-FINDINGS.md` | Checkpoint A: verified access findings, open questions, draft engineer messages. |
| `docs/FORMATS.md` | Proposed columns for the results report and the tag-number ledger. |

## Rules that bind all future code in this directory

- Read-only against client and Gryps data. No writes to PMWeb, ever.
- No credentials in code or committed files — environment variables only.
- One bad document must not kill a run; log a human-readable reason and continue.
- Never reprocess an already-run PCO unless explicitly told to re-run.
- Simple Python + CSV over clever infrastructure; written to be code-reviewed.

## Note on repository location

This project was intended to live in its own repository. The GitHub
integration used by this session does not have permission to create
repositories, so it is scaffolded here (in `pmweb-tm-review/` on a dedicated
branch of `mycela-actions-runner`) as a self-contained directory that can be
lifted into a new repo unchanged once one is created.
