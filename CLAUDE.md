# Handoff: Deploy the Catalog Integrity Monitor

You are deploying a daily Amazon catalog monitor for Designer iWear to Render.
Everything you need is in this folder. Work through the phases in order and
STOP at every checkpoint marked ⛔ to get the user's confirmation before
continuing.

## What this app does (context)

`catalog_monitor.py` is a daily cron job that:
1. Pulls the SP-API All Listings + FBA inventory reports
2. Maps child ASINs to parents via the Catalog Items API
3. Flags variation-integrity problems: orphans (no parent), lone children,
   split families (one model code across multiple parents)
4. Runs title QA on every parent with active inventory (auto-generated
   wording, "0MM", missing brand/model/lens size, casing, misspellings,
   >75 chars, outliers, missing main image, etc.)
5. Creates Asana tasks for Wen (assistant2@gefenoptical.com) on the
   "Amazon Title Fixes" board, multi-homed to the VAs project and Wen's board

It uses **Asana itself as the dedupe state store** (task names are
deterministic keys) and **aborts if the board reads back empty** unless
ALLOW_EMPTY_BOARD=1 — this is a safety feature, never "fix" it by removing
the check. Bundle SKUs (containing "BUNDLE") are intentionally parentless and
are excluded from orphan flagging — do not remove that exclusion either.

## Files in this folder

- `catalog_monitor.py` — the monitor (already written and syntax-checked; do
  not rewrite it, only deploy it)
- `requirements.txt` — `requests>=2.31.0`
- `.python-version` — `3.12.11` (pins Render's interpreter; Render's 3.14
  default breaks builds in this account's other repos — keep this file)
- `ignore.txt` — optional pre-suppression list, template only
- `CLAUDE.md` — this file (do NOT commit it to the repo)

## Phase 1 — Create the repo and push

1. Verify the four deploy files are present and `python3 -m py_compile
   catalog_monitor.py` passes.
2. Check `gh auth status`. If not authenticated, tell the user to run
   `gh auth login` and wait.
3. Create a fresh private repo and push ONLY the four deploy files
   (exclude CLAUDE.md):

   ```
   git init
   git add catalog_monitor.py requirements.txt .python-version ignore.txt
   git commit -m "Catalog integrity monitor: orphan detection + title QA"
   gh repo create catalog-monitor --private --source=. --push
   ```

   The user's GitHub account is `ahuva-rgb`. The old repo `asin-audit` is
   being abandoned (it has stray files) — do not reuse it, do not delete it
   without asking.
4. Confirm the repo root on GitHub shows exactly the four files.

⛔ CHECKPOINT: show the user the repo URL and file list before Phase 2.

## Phase 2 — Render cron job

Preferred path: Render API. Ask the user for a Render API key
(dashboard.render.com → Account Settings → API Keys). If they provide one,
export it as RENDER_API_KEY and create the service via the API
(https://api.render.com/v1/services, type `cron_job`). Note: Render's API
may require an `ownerId` — GET /v1/owners first.

If the user prefers not to create an API key, walk them through the
dashboard instead (New → Cron Job → connect `catalog-monitor`).

Either way, the service settings are:

- Runtime/Language: **Python 3**
- Build command: `pip install -r requirements.txt`
- Run command: `python catalog_monitor.py`
- Schedule: `0 10 * * *`  (daily 10:00 UTC ≈ 6am ET)
- Plan: cheapest available cron plan

Environment variables (ask the user for the values — NEVER echo them back,
never write them to any file, never commit them):

- `LWA_CLIENT_ID`
- `LWA_CLIENT_SECRET`
- `LWA_REFRESH_TOKEN`
  (same values as the existing removal-shipment automation on Render — the
  user can copy them from that service's Environment tab)
- `ASANA_PAT` — must be a freshly rotated token. Remind the user: a previous
  PAT was pasted into a chat once; if it was never rotated, rotate it now
  (Asana → Settings → Apps → Developer apps).
- Do NOT set `DRY_RUN` (defaults to on — required for first runs)
- Do NOT set `ALLOW_EMPTY_BOARD` (the board already has tasks; the empty-board
  abort is the duplicate-task safety)

⛔ CHECKPOINT: confirm service created and env vars set before Phase 3.

## Phase 3 — First dry run

1. Trigger a manual run (dashboard "Trigger Run", or POST
   /v1/services/{id}/jobs via API).
2. Watch the build log. Failure modes and fixes:
   - Log mentions Ruby/Gemfile → `requirements.txt` is not at repo root
   - Wheel-building/compiling for a long time → `.python-version` missing or
     ignored; interpreter must be 3.12.x
3. The run itself takes 10–20 min (Amazon report generation). Expected log
   shape: asana state counts → report polling → "parsed N listings rows" →
   catalog mapping progress → "X findings total, Y new after
   dedupe/suppression" → "DRY_RUN — no tasks created" + the list.
4. If it aborts on the empty-board check, STOP and show the user — do not
   set ALLOW_EMPTY_BOARD yourself.

⛔ CHECKPOINT: paste the findings list to the user. They review for false
positives. Adjustments to rules happen here (edit, commit, push, re-run).

## Phase 4 — Go live (only after the user explicitly approves)

1. Add env var `DRY_RUN=0` and trigger one manual run.
2. Verify in the log that tasks were created (≤12), then have the user
   check the Asana board: tasks in "To Fix", assigned to Wen, multi-homed.
3. Leave the daily schedule running.

## Ground rules

- Never print, log, or commit secret values.
- Do not modify the detection logic unless the user asks during Phase 3
  review.
- Do not remove: the empty-board abort, the 12-task cap, the bundle-SKU
  orphan exclusion, or the deterministic task-name format (it IS the dedupe
  key — changing it would duplicate the whole board).
- If anything is ambiguous, ask instead of guessing.
