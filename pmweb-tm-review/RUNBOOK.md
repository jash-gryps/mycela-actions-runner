# RUNBOOK — how to run the pipeline

**This is a skeleton.** Each section gets filled in with exact, click-by-click
/ command-by-command instructions as the corresponding stage is built and
approved at its checkpoint. Nothing below is runnable yet.

## Before you run anything

1. Credentials: the pipeline reads AWS credentials from environment variables
   (settings on your machine, outside the code). You get these from your AWS
   SSO login page ("Command line or programmatic access"). They expire after a
   few hours; if a command fails with an "expired token" error, refresh them
   and try again. Never paste them into a file in this project.
2. Confirm you are on the right machine/environment (to be decided in Phase 0,
   question 5).

## Stage 1 — Inventory
_To be written after Checkpoint B approval and stage build._

## Stage 2 — Prepare batch input (Agent Builder CSV)
_Blocked on Kyle confirming the exact CSV format._

## Stage 3 — Run the batch
_Blocked on confirming whether batch runs are API-triggerable or UI-only._

## Stage 4 — Collect results
_Blocked on confirming how Agent Builder exports results._

## Stage 5 — Report
_To be written._

## Stage 6 — Tag ledger
_To be written._

## When something fails

Every stage writes a plain-language log. Failures name the PCO and the reason
(e.g. "PCO 0212: attachment missing"), and the run continues past them. The
log location and what to do per failure type will be documented here per stage.
