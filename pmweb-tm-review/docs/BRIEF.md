# Project Brief — PMWeb Change Events → T&M Review Agent Pipeline

_As provided by the project owner (Jash, product/customer success), July 2026.
Preserved verbatim as the project's source of truth. Credentials that were
shared alongside it are deliberately NOT reproduced here._

---

I need you to build a pipeline that gets T&M PCO documents from the Change Events section of PMWeb and runs them through our T&M Review Agent in Gryps Agent Builder, then collects the results into a report I can read and share. This document gives you everything you need: who I am, the domain, the system landscape, what's verified vs. unverified, and the rules for how we'll work together. Read all of it before doing anything.

## 1. Who I am and how to work with me

I'm on the product/customer success side, not engineering. I cannot read code fluently and I cannot verify technical claims myself. That shapes everything:

* Explain every decision in plain language. Define each technical term in one line the first time you use it, then use it freely.
* Always label your claims as one of three things: KNOWN (you verified it or I told you), INFERRED (reasonable conclusion from evidence), or GUESS (assumption that needs confirmation). Never present a guess as a fact.
* When you hit something only an engineer can answer, stop and write me the exact question to send, addressed to the right person (Amir Tasbihi or Kyle Milden are our engineering leads).
* Give me your recommendation with reasoning and the main trade-off — not a neutral menu of options.
* Do not proceed past a checkpoint (defined in Section 9) without my explicit approval.

## 2. The goal

An end-to-end pipeline: identify the T&M PCO packages in the PMWeb Change Event module for three Massport projects → get those documents in front of our T&M Review Agent → run the agent on each one → collect the per-check results into a structured report → preserve the extracted tag/slip numbers in a separate ledger file for a future duplicate-detection workflow that someone else will build.

Success looks like: I can run one command (or follow a short runbook), and end up with a spreadsheet showing every T&M PCO across the three projects with a Pass / Fail / Needs Review result for each of the agent's checks, plus a clean handover-ready codebase an engineer could review and adopt.

## 3. Domain context (construction terms you need)

* Massport is the client — Massachusetts Port Authority, running construction projects at Logan Airport.
* CMAR (Construction Manager at Risk) is the delivery model: one construction manager holds the trade subcontracts.
* T&M (Time and Materials) means a subcontractor bills for actual labor hours and equipment used, rather than a fixed price. The alternative is Lump Sum (fixed price).
* A PCO (Potential Change Order) is a submitted change-order package. For T&M work, a PCO is a bundle: one cover sheet stating a total dollar amount, backed by individual slips — field tickets recording crew, hours, and equipment for a day of work. Slips can be clean typed PDFs or scans/photos of handwritten tickets. One PCO file can contain multiple cover-sheet-plus-slips sets.
* Each slip carries a tag number (also called slip number) — the printed identifier on the ticket. This is the business key for duplicate detection.
* PMWeb is the client's project management system (PMIS). The change-order records and their attached documents live in PMWeb's Change Event module.
* Gryps is our platform. It indexes client data. Relevant surfaces: Agent Builder (where document-review agents are configured — our T&M Review Agent lives here), Nexus (Gryps' chat-based AI assistant), Gryps Analytics (structured queries over indexed metadata), and Dojo (ML annotation/training — explicitly NOT used for this; don't route anything through it).

Projects in scope: L1697-C4, L1820, L1821 — all Massport CMAR projects.

People: Jonathan is the client stakeholder who requested this. Nadia is the client-side contact who supplies reviewed sample documents. Amir Tasbihi and Kyle Milden are our engineers.

## 4. The agent (already built — do not rebuild it)

The T&M Review Agent is already configured in Gryps Agent Builder with eight steps:

1. T&M vs. Lump Sum classification
2. T&M tag number extraction
3. Two-signature verification (contractor + owner's representative on each slip)
4. Per-slip hours extraction
5. Cover sheet hours extraction
6. Hours match validation (slip hours vs. cover sheet)
7. Cover sheet arithmetic check (slip totals sum to cover total, small rounding tolerance)
8. Sub-invoice backup verification

Design decisions already settled that the pipeline must respect:

* Output is three-state — Pass / Fail / Needs Review — not binary. The results report must carry all three states per check.
* The agent reviews one document at a time. Anything requiring comparison across documents (like duplicate detection) is out of its scope by design.
* One PCO file can contain multiple cover-sheet sets; the results format should not assume one set per file.

## 5. System landscape — what's verified and what isn't

This is the most important section. The biggest risk to this project is access, not code. Treat every item below with its label.

* KNOWN (from Kyle, our engineering lead): Agent Builder does NOT accept direct document uploads for batch runs. Batch runs are driven by a CSV file that points to documents already in Gryps. This means the pipeline's core job is probably: inventory the right documents → generate that CSV → run the batch → collect results. Confirm the exact CSV format with Kyle before building the generator.
* INFERRED (reported by Nexus, not independently verified): The data path is PMWeb → Gryps indexing → Nexus. There is no live PMWeb connector. "PMWeb is connected on our side" and "the Change Event documents are actually readable in the Massport Gryps tenant" are two different claims — as of mid-July the second one was still unverified.
* KNOWN: Gryps Analytics structured queries over Change Event metadata are the reliable way to count and list distinct T&M PCO packages. Nexus keyword search returns match-totals, not distinct packages — do not use it for inventory.
* GUESS (must confirm): Exact table and field names for Change Events in the Massport tenant, whether I have query access and through what interface, whether batch runs can be triggered by API or only through the UI, and how results are exported.

Three possible access scenarios — establish which is true before writing extraction code: (a) PMWeb Change Event documents are already indexed in the Massport Gryps tenant → pipeline queries Gryps. (b) They are not indexed → documents get exported manually from PMWeb (likely by Nadia or the project owner) → pipeline works from a folder of exports. (c) A new ingestion path needs to be built → that's engineering's project, not this pipeline's. Do NOT attempt to build a PMWeb scraper or direct PMWeb API integration without explicit instruction — that touches a client system.

## 6. Out of scope — do not build these

* Duplicate slip/tag detection. That is a separate, system-level workflow keyed on tag numbers, being handled independently. This pipeline's job is only to capture and output the extracted tag numbers cleanly (Section 8) so that workflow can consume them later. If you find yourself writing comparison logic across PCOs, stop.
* The file-existence / reverse-lookup feature (upload-a-file-to-check-if-it-exists). Architecturally a sibling, deliberately separate. Ignore it.
* Any modification of the agent's eight steps. If pipeline testing suggests an agent-side problem, report it; don't fix it.
* Anything write-access against PMWeb or client systems. The entire pipeline is read-only against client and Gryps data.

## 7. Phase 0 — Access reality check (hard gate before pipeline code)

Start here. Questions to answer (asking the project owner directly, drafting exact engineer messages where they don't know):

1. Which access scenario from Section 5 is true — are the Change Event records and their attachments for L1697-C4, L1820, and L1821 actually readable in the Massport Gryps tenant today?
2. Gryps Analytics: what are the table and field names for Change Event metadata in the Massport tenant? What field identifies T&M vs. other change events? Can queries be run, and through what — a UI, an API, credentials?
3. Agent Builder batch: exact CSV format (column names, a sample file if one exists), whether a batch run can be triggered programmatically or only via UI upload, and how results come out (export file? API? what format?).
4. Is there a Gryps/Nexus API callable from a script, with read-only credentials?
5. Where should this pipeline ultimately run — the project owner's laptop, or a Gryps environment? (Also check what's installed on the current machine.)

While answers are pending, only the parts independent of them may be built: the results-report format, the tag-number ledger format, and — once we have one sample — the CSV generator and results parser working against sample files. Do not build extraction code against a system whose readability is unconfirmed.

## 8. The pipeline — stages and deliverables

Architecture to be proposed at the Phase 1 checkpoint; it should cover these stages, roughly this shape:

1. Inventory — list every T&M PCO package in the Change Event module for the three projects, via whichever access path Phase 0 confirms. Output: a human-readable inventory file (project, PCO number, subcontractor, dollar amount, document reference).
2. Prepare batch input — generate the Agent Builder batch CSV from the inventory, in the exact confirmed format.
3. Run — trigger the batch (or, if it's UI-only, produce the CSV plus a step-by-step runbook of exactly what to click).
4. Collect — pull the agent's results and parse them: one row per PCO (or per cover-sheet set within a PCO), one column per check, three-state values.
5. Report — produce an Excel/CSV report: project, PCO number, subcontractor, total dollars, the eight check results, and an overall status.
6. Tag ledger — a separate file: project, subcontractor, PCO number, and every extracted tag/slip number. Capture only; no dedup logic.

Engineering standards, non-negotiable:

* State tracking: never reprocess a PCO that already ran, unless explicitly told to re-run. Keep a simple ledger of what's been processed (a small local file or database — choice explained in one plain sentence).
* Failure isolation: one bad or unreadable document must not kill the run. Log it with a readable reason ("PCO 0212: attachment missing", not a stack trace) and continue.
* No secrets in code: any credentials go in environment variables (settings stored on the machine, outside the code, so they never end up in shared files). Nothing sensitive gets committed or written into scripts.
* Boring and readable: prefer simple Python scripts and CSV files over clever infrastructure. Write everything as if Kyle will code-review it tomorrow, because he might. Include a plain-language README (what each file is, what it does) and a RUNBOOK (exactly how to run it, step by step, and what to do when something fails).

## 9. Working protocol — checkpoints and decision briefs

Loop: propose → approve → build a small piece → prove it works → advance. Checkpoints:

* Checkpoint A — after Phase 0: present what access we actually have, in a table of KNOWN / INFERRED / GUESS.
* Checkpoint B — architecture proposal: the full pipeline design in plain language, which stage depends on which Phase 0 answer, and the recommended build order.
* Checkpoint C — after each build stage: evidence it works on 1–3 real records (actual output, explained in plain terms) before scaling to everything.
* Checkpoint D — before any full run across all three projects.

At every checkpoint, and at any significant decision in between, a decision brief in exactly this format:

> Deciding: [the question in one sentence]
> Options: [2–3, one line each]
> My recommendation: [which one, and why, in plain language]
> Main trade-off: [what we give up]
> What could go wrong: [the realistic failure mode]
> Needs engineer confirmation: [yes/no — if yes, the exact question and who to ask]

Decision briefs may be taken to another Claude session (Fable) for a second opinion, with its advice pasted back. Treat that as senior technical review: reconcile it, state specifically where you agree and disagree and why, and update the plan only where the advice holds up. Do not defer to it automatically, and do not dismiss it — argue the merits.

## 10. Testing — before we trust anything

* The one verified ground-truth document is PCO 160 (The Dow Company: eight slips totaling $29,364.11 against a $29,364.00 cover sheet; all signatures present; math reconciles within 11 cents). It is a known-PASS case. Passing PCO 160 proves the pipeline plumbing works; it proves almost nothing about whether the agent catches problems, because it only exercises the pass path.
* The agreed test methodology is known-bad historical PCOs: documents the client already reviewed by hand and found problems in. Nadia is the source for client-reviewed examples. Build a small test harness that runs a set of known-good and known-bad PCOs and produces a comparison table — expected result vs. agent result, per check, per document.
* Flag any check where we have zero known-bad examples, because that check is effectively untested even if it shows green.

## 11. First actions

1. Confirm the brief is read and understood by summarizing the goal, the three access scenarios, and what's out of scope — in five sentences or fewer.
2. Check what's installed on the machine and report in plain language.
3. Start Phase 0: ask the Section 7 questions, and draft the engineer messages for whatever the project owner can't answer.

Do not write pipeline code until Checkpoint A and Checkpoint B are approved.
