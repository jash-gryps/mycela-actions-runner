# Phase 0 findings — access reality check (Checkpoint A)

_Last updated: 2026-07-15. Evidence gathered live from the Gryps AWS
environment (account 992382545670, us-east-1) using Jash's own AWS SSO
`data_analytics` role — i.e., everything marked KNOWN below was verified with
read-only queries, not taken on faith._

Plain-language glossary for this doc:
- **Athena** — AWS's SQL query service; you point it at cataloged data and run
  database-style queries. This appears to be what "Gryps Analytics" queries
  ride on.
- **Glue catalog** — AWS's directory of databases/tables that Athena can query.
- **SSO role** — the temporary login identity Jash gets from the company AWS
  sign-in page; it expires after a few hours and is refreshed by logging in
  again.

## The access-scenario verdict

Scenario (a) from the brief — "PMWeb Change Event data is already indexed in
the Massport Gryps tenant and the pipeline queries Gryps" — is **TRUE for the
structured records** (verified today). Whether it is also true for the
**attached documents** (the actual PCO PDFs, which is what Agent Builder needs)
is INFERRED-leaning-yes but not yet verified — see open question 1.

## KNOWN (verified today, or told to us directly)

| # | Finding | Evidence |
|---|---|---|
| K1 | Jash's AWS SSO `data_analytics` role can run Athena queries against Gryps' data catalog. | `sts get-caller-identity` + successful queries, 2026-07-15. |
| K2 | The Glue catalog has `pmweb` and `pmweb_db` databases; `pmweb_db` (107 tables) contains `cost_management_change_events`, `cost_management_change_event_details`, `cost_management_change_event_categories`, `projects`, `document_attachments`, `file_manager_files`, `file_link`. | Catalog listing, 2026-07-15. |
| K3 | All three projects exist and their Change Event records are readable: L1697-C4 → 339 change events, L1820 → 200, L1821 → 796 (none flagged deleted). A separate `L1820-DP4` project has 29 more. | Athena queries, 2026-07-15. |
| K4 | Key fields on `cost_management_change_events`: `record_number`, `project_id`, `description`, `ext_cost` (dollar amount), `company_id`, `category_id`, `create_date`. PCO numbers appear embedded in `description` text (e.g. "PCO#218 Select Demo — …"). | Schema + sample rows, 2026-07-15. |
| K5 | `category_id` does NOT mark T&M. Categories in use on these projects are budget buckets: "Owner Contingency", "Allowance / Holds", "CM Contingency", or blank. There is no visible structured field saying T&M vs. Lump Sum. | Category counts query, 2026-07-15. |
| K6 | Data quirk: L1697-C4's `project_number` is stored with trailing spaces (`"L1697-C4  "`). Queries must trim or use prefix matching. | Projects query, 2026-07-15. |
| K7 | Agent Builder batch runs are driven by a CSV pointing at documents already in Gryps; no direct uploads. (From Kyle, per the brief.) | Brief, Section 5. |
| K8 | This session's machine has Python 3.11, pip, git, and boto3 (the Python AWS library) working through the network proxy. | Checked 2026-07-15. |

## INFERRED (reasonable, not yet verified)

| # | Inference | Basis | How to verify |
|---|---|---|---|
| I1 | The attached PCO documents are also reachable: `document_attachments` links change events to files, and `file_manager_files`/`file_link` carry file names/GUIDs, suggesting document binaries live in Gryps-managed storage (likely one of the S3 buckets seen, e.g. the `rms-storage` file bucket). | Table schemas. | Ask Kyle (question 1 below) or query attachment counts for our change events. |
| I2 | "Gryps Analytics" is this same Athena/Glue stack, so pipeline queries here ARE the sanctioned Gryps Analytics path, not a side door. | The role is literally named `data_analytics`; workgroups like `analytics-maintenance-prod` exist. | Confirm with Kyle/Amir. |
| I3 | T&M vs. Lump Sum is probably only determinable from the documents themselves (which is exactly the agent's check #1), or from text conventions in `description` — meaning the inventory stage may need to cast a wider net and let the agent classify. | K5. | Ask Jonathan/Nadia if there's a naming convention; ask Kyle if another field encodes it. |

## GUESS (unconfirmed, blocking specific stages)

| # | Open item | Blocks |
|---|---|---|
| G1 | Exact Agent Builder batch CSV format (columns, what a "document pointer" looks like), and whether runs are API-triggerable or UI-only. | Stages 2–3. |
| G2 | How Agent Builder results are exported (file? API? format?), including how multiple cover-sheet sets per file are represented. | Stages 4–5, and the FORMATS.md column mapping. |
| G3 | Whether `document_attachments` rows exist for our change events, and how a `file_id`/GUID maps to the identifier Agent Builder's CSV expects. | Stage 1 → 2 handoff. |
| G4 | Where the pipeline should ultimately run (Jash's laptop with SSO creds vs. a Gryps environment). | Packaging/runbook. |
| G5 | Whether L1820-DP4 is in scope under "L1820". | Inventory scope. |

## Questions for Jash (project owner)

1. Is **L1820-DP4** (29 change events, separate project record) part of what
   Jonathan means by "L1820"?
2. Do you know of any naming convention that marks a PCO as T&M (e.g. "T&M"
   in the title), or should we plan to inventory all PCOs and let the agent's
   check #1 sort T&M from Lump Sum?
3. Where do you want to run this day-to-day — your laptop, or somewhere Gryps
   hosts? (Your AWS SSO login works from a laptop; that's the simplest.)

## Draft messages to engineering

### To Kyle (Agent Builder + documents)

> Hi Kyle — for the PMWeb T&M review pipeline: I can already query the Change
> Event records for L1697-C4/L1820/L1821 in Athena (`pmweb_db` database) with
> my data_analytics role, so inventory is unblocked. Three things I need from
> you to build the batch step:
> 1. The exact batch CSV format Agent Builder expects — column names and, if
>    you have one, a real sample file. Specifically: what does the "document
>    pointer" column contain, and how does it relate to `pmweb_db.document_attachments.file_id`
>    or the file GUIDs in `file_manager_files`?
> 2. Can a batch run be kicked off programmatically (API/CLI), or is it
>    UI-upload only? If UI-only that's fine — I just need to know which to
>    build for.
> 3. How do results come out — export file, API, what format — and how does it
>    represent a PCO file containing multiple cover-sheet sets?
> Also a sanity check: are the PMWeb attachment binaries for Massport actually
> in Gryps storage and readable by Agent Builder today, or only the metadata?

### To Amir (access + hosting)

> Hi Amir — quick confirm for the PMWeb T&M pipeline: I'm using my AWS SSO
> data_analytics role to run read-only Athena queries on `pmweb_db` for the
> Massport projects. (1) Is that the sanctioned way to do "Gryps Analytics"
> queries from a script, or is there an API you'd rather I use? (2) The
> pipeline is simple Python + CSV, read-only; where would you want it to live
> long-term — run from my laptop with SSO creds, or checked into a Gryps repo
> and run in one of our environments?

## Security note (logged so it isn't forgotten)

AWS session credentials were pasted into the working session on 2026-07-15.
They are temporary (SSO) and expire on their own, but as hygiene: prefer not
to paste secrets into chat sessions; going forward the pipeline reads them
from environment variables only, and nothing secret is committed to this
repository.
