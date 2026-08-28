# The Neon compute budget

This project runs on Neon's **free** plan, per the `$0 cost` constraint. The
scarcest resource in the whole system is not Actions minutes, storage, or
bandwidth — it is **Neon compute time**. This document exists because we
exhausted it once and it took the platform down.

## What happened (2026-08-25)

The monthly compute quota ran out. Neon then refused *every* connection with:

```
ERROR: Your account or project has exceeded the compute time quota.
```

Consequences, none of which named the real cause:

| Symptom | Why it looked unrelated |
|---|---|
| Every dashboard login failed | `authorize()` could not distinguish a dead database from a wrong password, so it rendered "Invalid username or password" for everyone |
| The notebook fleet stopped | Last run was #1678 at 01:01Z; the scheduler could not read its schedule |
| No alert fired | Nothing watched consumption |

It was found three days later, by a user who could not sign in.

## The one mechanic you must understand

**Neon bills for how long a compute endpoint stays AWAKE — not for how many
queries you run.**

An endpoint suspends after `suspend_timeout_seconds` of idle. So:

> Anything that touches the database more often than the suspend timeout keeps
> the endpoint awake permanently, and costs the same as a 24/7 server.

One query a minute and ten thousand queries a minute cost roughly the same. What
matters is the *gap* between queries.

That is precisely how the quota went. `heartbeat.yml` ticked every ~2.5 minutes
against a 300-second suspend timeout. The gap was always smaller than the
timeout, so the endpoint never suspended once — pinned awake at the 0.25 CU
floor, ~6 CU-hours/day, ~180 CU-h/month against a ~100 CU-h budget.

## The budget

At the 0.25 CU autoscale floor, a ~100 CU-hour monthly quota is about
**400 awake-hours/month ≈ 13 hours/day**. Measured contributors:

| Source | Awake time | Note |
|---|---|---|
| 41 notebook runs/day × ~4.6 min | ~3.8 h/day (at 60s suspend) | irreducible; this is the actual work |
| Heartbeat @ 15 min tick | ~1.6 h/day | was ~11.5 h/day at a 2.5 min tick |
| Dashboard polling | small, only while a tab is open | was unbounded before the visibility fix |
| **Total** | **~5.4 h/day ≈ 41 CU-h/month** | ~2.4× headroom under the budget |

## The two settings that keep this safe

Both must hold. Either one alone is NOT enough — at a 2.5-min tick a 60s
timeout still projects ~101 CU-h/month, and at a 300s timeout a 15-min tick
still projects ~109 CU-h/month.

1. **Endpoint `suspend_timeout_seconds` = 60** (not Neon's 300s default).
2. **Heartbeat tick interval = 15 min** (`sleep 900` in `heartbeat.yml`),
   comfortably above the suspend timeout.

## Rules for any new code

1. **Never poll the database on a loop shorter than the suspend timeout.** If you
   need a fast loop, make the loop itself database-free and only connect when
   there is real work to do.
2. **A "free" outer resource does not make the inner one free.** The heartbeat's
   comment correctly noted that a public repo has unlimited Actions minutes —
   but each of those free minutes opened a *metered* Neon connection. Follow the
   call chain to the most expensive thing it touches.
3. **Any always-on client needs a cost review**, not just a correctness review.
4. **Prefer event-driven over polled.** Fire on demand rather than asking
   "is there work?" on a timer.
5. **Never report an infrastructure failure as a user error.** The login bug
   turned a total outage into "your password is wrong", which is why this cost
   three days instead of ten minutes.

## The guard

`.github/workflows/neon_budget.yml` runs `scripts/check_neon_budget.py` daily.
It projects this period's usage to the period end and fails if the projection
exceeds 80% of budget; GitHub emails the owner on a failed scheduled workflow.
It is stdlib-only and read-only, so it still works while the quota is exhausted.

Required secrets: `NEON_API_KEY` (read-only use), `NEON_PROJECT_ID`. Without
them the check skips rather than failing the build — so **confirm it is actually
configured**, or the early warning silently does not exist.

Run it by hand any time:

```bash
NEON_API_KEY=... NEON_PROJECT_ID=... python scripts/check_neon_budget.py
```

Against the August 2026 numbers this check would have failed on **2026-08-02** —
23 days before the outage — because the run-rate was over budget from day one.
