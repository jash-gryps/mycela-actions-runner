# Proposed output formats

_Status: proposed, awaiting approval. These are the two deliverable files the
brief allows us to design before Phase 0 answers land (Section 7). Formats may
gain columns once Kyle confirms the Agent Builder results format, but should
not lose any._

## 1. Results report (`results_report.csv` / `.xlsx`)

One row per **cover-sheet set**, not per PCO file — because one PCO file can
contain multiple cover-sheet-plus-slips sets (brief, Section 4). A PCO with a
single set is simply one row where `set_number` = 1 of 1.

| Column | Meaning | Example |
|---|---|---|
| `project` | Project number as in PMWeb | `L1697-C4` |
| `pco_number` | The PCO identifier | `160` |
| `record_number` | PMWeb change event record number (system key, for traceability) | `0000175` |
| `subcontractor` | Company that submitted the work | `The Dow Company` |
| `set_number` | Which cover-sheet set within the PCO file (1-based) | `1` |
| `sets_in_file` | Total cover-sheet sets found in the file | `1` |
| `total_dollars` | Cover sheet total for this set | `29364.00` |
| `check_1_tm_classification` | Pass / Fail / Needs Review | `Pass` |
| `check_2_tag_extraction` | Pass / Fail / Needs Review | `Pass` |
| `check_3_two_signatures` | Pass / Fail / Needs Review | `Pass` |
| `check_4_slip_hours` | Pass / Fail / Needs Review | `Pass` |
| `check_5_cover_hours` | Pass / Fail / Needs Review | `Pass` |
| `check_6_hours_match` | Pass / Fail / Needs Review | `Pass` |
| `check_7_arithmetic` | Pass / Fail / Needs Review | `Pass` |
| `check_8_subinvoice_backup` | Pass / Fail / Needs Review | `Needs Review` |
| `overall_status` | `Pass` if all eight pass; `Fail` if any check fails; otherwise `Needs Review` | `Needs Review` |
| `document_reference` | Pointer to the reviewed document in Gryps (exact form TBD with Kyle) | TBD |
| `run_id` | Which batch run produced this row (date-based) | `2026-07-20_run1` |
| `notes` | Plain-language processing notes ("attachment missing", "skipped: already processed") | |

Rules:
- The three states are spelled exactly `Pass`, `Fail`, `Needs Review`.
- A document that could not be processed at all still gets a row: every check
  column set to `Needs Review` and the reason in `notes`, so nothing silently
  disappears from the report.

## 2. Tag-number ledger (`tag_ledger.csv`)

One row per **extracted tag/slip number**. Capture only — no duplicate
detection logic lives here (brief, Section 6); this file is the input a future
dedup workflow will consume.

| Column | Meaning | Example |
|---|---|---|
| `project` | Project number | `L1697-C4` |
| `subcontractor` | Company on the slip | `The Dow Company` |
| `pco_number` | PCO the slip belongs to | `160` |
| `record_number` | PMWeb change event record number | `0000175` |
| `set_number` | Cover-sheet set within the PCO file | `1` |
| `tag_number` | The tag/slip number exactly as extracted, no normalization | `T-04512` |
| `extraction_confidence` | As reported by the agent, if available (else blank) | |
| `run_id` | Which batch run extracted it | `2026-07-20_run1` |

Rules:
- Tag numbers are recorded verbatim (dedup keying and normalization decisions
  belong to the downstream workflow's owner).
- A slip whose tag could not be read gets a row with `tag_number` empty and
  the reason in a `notes` column — so the dedup workflow knows a slip existed
  but its key is missing, rather than the slip being invisible.

## 3. Processed-state ledger (internal, not a deliverable)

A small local file (`state/processed.csv`) recording `record_number`,
`document_reference`, `run_id`, and outcome — this is how the pipeline avoids
reprocessing PCOs that already ran. A plain CSV was chosen over a database
because anyone can open and fix it in Excel.
