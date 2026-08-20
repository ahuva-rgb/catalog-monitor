#!/usr/bin/env python3
"""
Catalog Integrity Monitor — Designer iWear
==========================================
Daily Render Cron Job. Two jobs in one pass:

  A) VARIATION INTEGRITY — ASINs that have fallen off (or never joined) their
     parent: orphans, lone children, disowned children, split families,
     families that were never built.

  B) PARENT TITLE QA — parents whose titles look wrong: Amazon auto-generated
     wording ("Man Sunglasses", "0MM"), missing brand / model code / lens size,
     junk text, >75 chars, ALL CAPS, repeated words, bad model-code casing
     (Rb2215f -> RB2215F), brand misspellings (Rayban -> Ray-Ban), plus
     statistical outliers vs. the rest of the catalog.

Flagged issues become Asana tasks assigned to Wen in "Amazon Title Fixes",
multi-homed into the VAs project and Wen's own board (team rule).

STATE LIVES IN ASANA — no database, no disk. Before creating anything, the
script reads the entire board and dedupes against existing task names, which
are deterministic per finding. Tasks Wen drags into any section whose name
contains "false positive", "won't fix", or "skip" are permanently suppressed.

SAFETY: if the board read returns zero sections or zero tasks (or a section
fetch fails), the script ABORTS instead of re-creating every task as a
duplicate. Set ALLOW_EMPTY_BOARD=1 only for a genuine first run.

ENVIRONMENT VARIABLES
  LWA_CLIENT_ID, LWA_CLIENT_SECRET, LWA_REFRESH_TOKEN   Amazon SP-API (LWA)
  ASANA_PAT                                             Asana personal token
  DRY_RUN            "1" (default) = log findings, create nothing in Asana
  ALLOW_EMPTY_BOARD  "1" = permit an empty board read (first run only)
  MAX_TASKS_PER_RUN  default 12
"""

import io
import csv
import json
import os
import re
import sys
import time
import gzip
import urllib.parse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SP_API_HOST = "https://sellingpartnerapi-na.amazon.com"
MARKETPLACE_ID = "ATVPDKIKX0DER"  # amazon.com

LISTINGS_REPORT = "GET_MERCHANT_LISTINGS_ALL_DATA"
INVENTORY_REPORT = "GET_FBA_MYI_UNSUPPRESSED_INVENTORY_DATA"
INVENTORY_FALLBACK_HOURS = 4   # reuse a completed inventory report this fresh
FINDINGS_CSV = "findings.csv"

ASANA_HOST = "https://app.asana.com/api/1.0"
WORKSPACE_GID = "1154118930825701"
TITLE_FIXES_PROJECT = "1217263070739951"   # "Amazon Title Fixes" board
TO_FIX_SECTION = "1217249100522206"        # default landing section
VAS_PROJECT = "1207030356476141"           # multi-home: VAs project
WEN_BOARD = "1207647962878260"             # multi-home: Wen's own board
ASSIGNEE_EMAIL = "assistant2@gefenoptical.com"  # Wen

DRY_RUN = os.environ.get("DRY_RUN", "1") == "1"
ALLOW_EMPTY_BOARD = os.environ.get("ALLOW_EMPTY_BOARD", "0") == "1"
MAX_TASKS_PER_RUN = int(os.environ.get("MAX_TASKS_PER_RUN", "12"))

SUPPRESS_SECTION_HINTS = ("false positive", "won't fix", "wont fix", "skip")

TITLE_MAX_LEN = 75

BRAND_CANONICAL = {
    "rayban": "Ray-Ban",
    "ray ban": "Ray-Ban",
    "oakly": "Oakley",
    "persoll": "Persol",
}

KNOWN_BRANDS = (
    "Ray-Ban", "Oakley", "Persol", "Versace", "Arnette", "Vogue",
    "Dolce", "Ralph Lauren", "Costa", "Prada", "Burberry", "Michael Kors",
    "Emporio Armani", "Armani Exchange", "Miu Miu", "Oliver Peoples",
    "Spy", "Carrera", "Under Armour",
    # Aug 20 2026: these were firing NO_BRAND on perfectly good titles.
    "Quay", "Bolle", "Serengeti", "Revo", "Adidas", "Marc Jacobs",
    "Bottega Veneta", "Ray-Ban Meta", "Bottega",
)

# Products that do not follow the sunglass title convention. Checked in order,
# first match wins, so LENSES beats GOGGLES for "Ski Goggles Replacement Lenses".
CATEGORY_PATTERNS = (
    ("LENSES",  ("replacement lens", "replacement lenses")),
    ("GOGGLES", ("ski goggle", "snow goggle", "goggle")),
    ("HELMET",  ("snow helmet", "ski helmet", "helmet")),
    ("SMART",   ("meta hstn", "meta vanguard", "meta glasses", "smart glasses",
                 "ray-ban stories")),
    ("OPTICAL", ("eyeglasses",)),
)

# Goggles, helmets and smart glasses have no model code or lens size by
# convention; replacement lenses do carry the code but never a lens size.
NO_MODEL_EXEMPT = {"GOGGLES", "HELMET", "SMART"}
NO_LENS_SIZE_EXEMPT = {"LENSES", "GOGGLES", "HELMET", "SMART"}

# Model-code shapes per brand family (RB2132, ORB4165, OO9102, PO3019S, VE4361…)
MODEL_CODE_RE = re.compile(
    r"\b(?:"
    r"MARC[0-9]{2,5}"          # Marc Jacobs runs short: Marc59, Marc096
    r"|(?:RB|ORB|RX|RJ|OO|OJ|OX|PO|VE|VK|AN|VO|DG|DX|RA|RL|PH|PP|6S|9S|HC|MK"
    r"|EA|AX|SPS|SPR|SP|BE|OV|BV|UA|CA)[0-9]{3,5}"
    r")[A-Z]{0,3}\b",
    re.IGNORECASE,
)

# These brands identify models by NAME, not a numeric code, so a missing
# code is not a defect. Oakley is deliberately NOT here: it carries both.
NAME_ONLY_BRANDS = ("quay", "bolle", "serengeti", "revo")

# Oakley model names count as a valid model reference even without a code
OAKLEY_MODEL_NAMES = (
    "holbrook", "sutro", "flak", "radar", "gascan", "frogskins", "half jacket",
    "jawbreaker", "turbine", "sliver", "ev zero", "evzero", "latch", "hydra",
    "actuator", "leffingwell", "sylas", "portal", "kaast", "resistor", "wheel house",
    # Aug 20 2026: pulled from the live catalog. Oakley titles must carry a
    # name AND a code, so anything missing here becomes a false NO_MODEL_NAME.
    "split time", "sielo", "spindrift", "feedback", "unstoppable", "holston",
    "drop point", "straight jacket", "jupiter squared", "racing jacket",
    "fuel cell", "flight deck", "target line", "fall line", "flow scape",
    "o-frame", "line miner", "batwolf", "capacitor", "plate", "meta hstn",
    "meta vanguard", "mod1", "mod3", "mod7", "mod bc",
)

LENS_SIZE_RE = re.compile(r"\b[3-8][0-9]\s?mm\b", re.IGNORECASE)
ALWAYS_UPPER_TOKENS = ("uv", "rx", "hd")

JUNK_PATTERNS = (
    "clothing, shoes & jewelry",
    "amazon.com",
    "see size options",
    "asin:",
)

# SKUs matching this are never flagged as orphans. Bundle SKUs (*BUNDLE-RB,
# *BUNDLE-OO, *BUNDLE-AN…) intentionally sit outside variation families —
# their "parent" rows can appear Inactive in reports, so structural orphan
# logic misreads them. (Aug 19 2026 finding.)
ORPHAN_EXCLUDE_SKU_RE = re.compile(r"BUNDLE", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def die(msg, code=1):
    log(f"FATAL: {msg}")
    sys.exit(code)


def require_env(*names):
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        die(f"missing environment variables: {', '.join(missing)}")


# ---------------------------------------------------------------------------
# Amazon SP-API
# ---------------------------------------------------------------------------

def lwa_token():
    r = requests.post(
        "https://api.amazon.com/auth/o2/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": os.environ["LWA_REFRESH_TOKEN"],
            "client_id": os.environ["LWA_CLIENT_ID"],
            "client_secret": os.environ["LWA_CLIENT_SECRET"],
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def sp_get(token, path, params=None):
    for attempt in range(6):
        r = requests.get(
            SP_API_HOST + path,
            params=params or {},
            headers={"x-amz-access-token": token},
            timeout=60,
        )
        if r.status_code == 429:
            wait = 2 ** attempt
            log(f"SP-API throttled on {path}, waiting {wait}s")
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()
    die(f"SP-API throttling never cleared on {path}")


def sp_post(token, path, body):
    for attempt in range(6):
        r = requests.post(
            SP_API_HOST + path,
            json=body,
            headers={"x-amz-access-token": token},
            timeout=60,
        )
        if r.status_code == 429:
            time.sleep(2 ** attempt)
            continue
        r.raise_for_status()
        return r.json()
    die(f"SP-API throttling never cleared on {path}")


def request_report(token, report_type):
    res = sp_post(token, "/reports/2021-06-30/reports", {
        "reportType": report_type,
        "marketplaceIds": [MARKETPLACE_ID],
    })
    return res["reportId"]


def wait_for_report(token, report_id, label, timeout_min=45, fatal=True):
    """Reports can take 5-15 min to generate; poll until DONE.

    With fatal=False, a failed or timed-out report returns None instead of
    exiting, so the caller can retry or fall back.
    """
    deadline = time.time() + timeout_min * 60
    while time.time() < deadline:
        res = sp_get(token, f"/reports/2021-06-30/reports/{report_id}")
        status = res.get("processingStatus")
        if status == "DONE":
            return res["reportDocumentId"]
        if status in ("CANCELLED", "FATAL"):
            if not fatal:
                log(f"report {label} ended with status {status}")
                return None
            die(f"report {label} ended with status {status}")
        log(f"report {label}: {status}, waiting…")
        time.sleep(45)
    if not fatal:
        log(f"report {label} did not finish within {timeout_min} minutes")
        return None
    die(f"report {label} did not finish within {timeout_min} minutes")


def _parse_iso8601(value):
    """SP-API timestamps are ISO 8601, usually Z-suffixed."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def find_recent_report(token, report_type, max_age_hours):
    """Newest already-DONE report of this type created within max_age_hours.

    Returns (document_id, created_time) or (None, None).
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    res = sp_get(token, "/reports/2021-06-30/reports", {
        "reportTypes": report_type,
        "processingStatuses": "DONE",
        "marketplaceIds": MARKETPLACE_ID,
        "createdSince": cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pageSize": 100,
    })
    best_doc, best_time = None, None
    for rep in res.get("reports", []):
        doc = rep.get("reportDocumentId")
        created = _parse_iso8601(rep.get("createdTime"))
        if not doc or created is None or created < cutoff:
            continue
        if best_time is None or created > best_time:
            best_doc, best_time = doc, created
    return best_doc, best_time


def recover_inventory_report(token):
    """Inventory report failed: retry once, then reuse a recent one."""
    log("fba-inventory failed — retrying once in 60s")
    time.sleep(60)
    retry_id = request_report(token, INVENTORY_REPORT)
    doc = wait_for_report(token, retry_id, "fba-inventory-retry", fatal=False)
    if doc:
        return doc

    log(f"retry failed — looking for a completed inventory report from the "
        f"last {INVENTORY_FALLBACK_HOURS}h")
    doc, created = find_recent_report(token, INVENTORY_REPORT,
                                      INVENTORY_FALLBACK_HOURS)
    if doc:
        age_min = int((datetime.now(timezone.utc) - created).total_seconds() // 60)
        log(f"reusing inventory report from {created.isoformat()} ({age_min} min old)")
        return doc

    die("fba-inventory report failed twice and no report from the last "
        f"{INVENTORY_FALLBACK_HOURS}h is available")


def download_report(token, document_id):
    doc = sp_get(token, f"/reports/2021-06-30/documents/{document_id}")
    r = requests.get(doc["url"], timeout=120)
    r.raise_for_status()
    raw = r.content
    if doc.get("compressionAlgorithm") == "GZIP":
        raw = gzip.decompress(raw)
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def parse_tsv(text):
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return []
    header = lines[0].split("\t")
    rows = []
    for line in lines[1:]:
        cells = line.split("\t")
        rows.append({header[i].strip().lower(): (cells[i] if i < len(cells) else "")
                     for i in range(len(header))})
    return rows


def col(row, *names):
    for n in names:
        if n in row and row[n] != "":
            return row[n]
    return ""


# ---------------------------------------------------------------------------
# Catalog Items — child -> parent mapping
# ---------------------------------------------------------------------------

def catalog_batch(token, asins):
    """Fetch relationships + summaries + images for up to 20 ASINs."""
    out = {}
    params = {
        "identifiers": ",".join(asins),
        "identifiersType": "ASIN",
        "marketplaceIds": MARKETPLACE_ID,
        "includedData": "relationships,summaries,images",
        "pageSize": 20,
    }
    res = sp_get(token, "/catalog/2022-04-01/items", params)
    for item in res.get("items", []):
        asin = item.get("asin")
        parent = None
        for rel_block in item.get("relationships", []):
            for rel in rel_block.get("relationships", []):
                if rel.get("type") == "VARIATION" and rel.get("parentAsins"):
                    parent = rel["parentAsins"][0]
        title = ""
        for s in item.get("summaries", []):
            title = s.get("itemName") or title
        has_image = any(img.get("images") for img in item.get("images", []))
        out[asin] = {"parent": parent, "title": title, "has_image": has_image}
    return out


def map_parents(token, asins):
    result = {}
    asins = list(asins)
    for i in range(0, len(asins), 20):
        chunk = asins[i:i + 20]
        result.update(catalog_batch(token, chunk))
        time.sleep(0.6)  # stay under Catalog Items rate limit
        if i and i % 200 == 0:
            log(f"catalog mapping: {i}/{len(asins)}")
    return result


# ---------------------------------------------------------------------------
# Title QA rules
# ---------------------------------------------------------------------------

def product_category(title):
    """Coarse product category from the title; SUN is the sunglass default."""
    tl = (title or "").lower()
    for category, needles in CATEGORY_PATTERNS:
        if any(n in tl for n in needles):
            return category
    return "SUN"


def title_findings(title, brand_hint=None, child_model_codes=None, has_image=True):
    """Return list of (code, human_label) issues for one parent title."""
    issues = []
    t = title or ""
    tl = t.lower()
    category = product_category(t)

    if not t.strip():
        return [("TITLE_EMPTY", "Title is empty")]

    if re.search(r"\b(man|woman)\s+sunglasses\b", tl):
        issues.append(("AUTOGEN_WORDING", 'Amazon auto-generated wording ("Man/Woman Sunglasses")'))
    if "0mm" in tl.replace(" ", ""):
        issues.append(("ZERO_MM", '"0MM" lens size'))
    for junk in JUNK_PATTERNS:
        if junk in tl:
            issues.append(("JUNK_TEXT", f'Junk text: "{junk}"'))
            break

    if not any(b.lower() in tl for b in KNOWN_BRANDS):
        issues.append(("NO_BRAND", "No recognizable brand name"))

    for wrong, right in BRAND_CANONICAL.items():
        if re.search(rf"\b{re.escape(wrong)}\b", tl) and right.lower() not in tl:
            issues.append(("BRAND_SPELLING", f'Brand misspelled: should be "{right}"'))

    model_codes = MODEL_CODE_RE.findall(t)
    is_oakley_name = any(name in tl for name in OAKLEY_MODEL_NAMES)
    if category not in NO_MODEL_EXEMPT:
        if "oakley" in tl:
            # Oakley titles carry both halves: "Flak 2.0 XL OO9188".
            if not model_codes:
                issues.append(("NO_MODEL_CODE",
                               "Oakley title has no model code (e.g. OO9188)"))
            if not is_oakley_name:
                issues.append(("NO_MODEL_NAME",
                               "Oakley title has no model name (e.g. Holbrook)"))
        elif any(b in tl for b in NAME_ONLY_BRANDS):
            pass  # the model name is the identifier for these brands
        elif not model_codes:
            issues.append(("NO_MODEL", "No model code or model name"))

    # bad casing: code present but not fully uppercase (Rb2215f)
    for code in model_codes:
        if code != code.upper():
            issues.append(("MODEL_CASING", f'Model code casing: "{code}" should be "{code.upper()}"'))
            break

    is_meta = "meta" in tl and ("smart" in tl or "wayfarer" in tl or "headliner" in tl or "skyler" in tl or "glasses" in tl)
    if (not LENS_SIZE_RE.search(t) and not is_meta
            and category not in NO_LENS_SIZE_EXEMPT):
        issues.append(("NO_LENS_SIZE", "No lens size (e.g. 55mm)"))

    if len(t) > TITLE_MAX_LEN:
        issues.append(("TOO_LONG", f"Title is {len(t)} chars (max {TITLE_MAX_LEN})"))

    letters = [c for c in t if c.isalpha()]
    if letters and sum(c.isupper() for c in letters) / len(letters) > 0.7 and len(t) > 20:
        issues.append(("ALL_CAPS", "Title is mostly ALL CAPS"))

    words = [w.lower() for w in re.findall(r"[A-Za-z]{3,}", t)]
    counts = Counter(words)
    repeated = [w for w, c in counts.items() if c >= 3]
    if repeated:
        issues.append(("REPEATED_WORDS", f"Repeated words: {', '.join(repeated[:3])}"))

    for tok in ALWAYS_UPPER_TOKENS:
        if re.search(rf"\b{tok}\b", t) and not re.search(rf"\b{tok.upper()}\b", t):
            issues.append(("TOKEN_CASING", f'"{tok.upper()}" should be uppercase'))
            break

    # parent model code should match majority of children
    if child_model_codes:
        parent_codes = {c.upper() for c in model_codes}
        majority = Counter(c.upper() for c in child_model_codes).most_common(1)
        if majority and parent_codes and majority[0][0] not in parent_codes:
            issues.append(("MODEL_MISMATCH",
                           f"Parent model code doesn't match children (children mostly {majority[0][0]})"))

    if not has_image:
        issues.append(("NO_MAIN_IMAGE", "No main image"))

    return issues


def structure_signature(title):
    """Coarse title shape used for the <2% statistical outlier check."""
    t = title or ""
    sig = []
    sig.append("BRAND" if any(b.lower() in t.lower() for b in KNOWN_BRANDS) else "-")
    sig.append("MODEL" if MODEL_CODE_RE.search(t) else "-")
    sig.append("MM" if LENS_SIZE_RE.search(t) else "-")
    sig.append(str(min(len(t) // 25, 4)))
    return "|".join(sig)


# ---------------------------------------------------------------------------
# Asana
# ---------------------------------------------------------------------------

def asana_headers():
    return {"Authorization": f"Bearer {os.environ['ASANA_PAT']}"}


def asana_get_all(path, params=None):
    """Cursor-paginated GET returning every record."""
    params = dict(params or {})
    params["limit"] = 100
    out = []
    while True:
        r = requests.get(ASANA_HOST + path, params=params,
                         headers=asana_headers(), timeout=60)
        r.raise_for_status()
        body = r.json()
        out.extend(body.get("data", []))
        nxt = (body.get("next_page") or {}).get("offset")
        if not nxt:
            return out
        params["offset"] = nxt


def read_board_state():
    """
    Returns (existing_names, suppressed_names).
    Aborts on a suspiciously empty read unless ALLOW_EMPTY_BOARD=1.
    """
    try:
        sections = asana_get_all(f"/projects/{TITLE_FIXES_PROJECT}/sections")
    except Exception as e:
        die(f"could not read board sections — refusing to continue ({e})")

    if not sections and not ALLOW_EMPTY_BOARD:
        die("board returned ZERO sections. If this is truly a fresh board, "
            "set ALLOW_EMPTY_BOARD=1 for this one run. Otherwise this is an "
            "API glitch and continuing would duplicate every task.")

    suppress_gids = [s["gid"] for s in sections
                     if any(h in s.get("name", "").lower() for h in SUPPRESS_SECTION_HINTS)]

    try:
        tasks = asana_get_all(f"/projects/{TITLE_FIXES_PROJECT}/tasks",
                              {"opt_fields": "name,completed,completed_at"})
    except Exception as e:
        die(f"could not read board tasks — refusing to continue ({e})")

    if not tasks and not ALLOW_EMPTY_BOARD:
        die("board returned ZERO tasks. Set ALLOW_EMPTY_BOARD=1 only if this "
            "is a genuine first run on an empty board.")

    existing = {t["name"] for t in tasks if t.get("name")}

    suppressed = set()
    for gid in suppress_gids:
        try:
            sec_tasks = asana_get_all(f"/sections/{gid}/tasks", {"opt_fields": "name"})
        except Exception as e:
            die(f"could not read suppress section {gid} — refusing to continue ({e})")
        suppressed.update(t["name"] for t in sec_tasks if t.get("name"))

    return existing, suppressed


def find_assignee_gid():
    users = asana_get_all(f"/workspaces/{WORKSPACE_GID}/users", {"opt_fields": "email,name"})
    for u in users:
        if (u.get("email") or "").lower() == ASSIGNEE_EMAIL:
            return u["gid"]
    log(f"WARNING: could not find {ASSIGNEE_EMAIL} in workspace; tasks will be unassigned")
    return None


def create_task(name, notes, assignee_gid):
    body = {
        "data": {
            "name": name,
            "notes": notes,
            "projects": [TITLE_FIXES_PROJECT, VAS_PROJECT, WEN_BOARD],
            "memberships": [
                {"project": TITLE_FIXES_PROJECT, "section": TO_FIX_SECTION},
            ],
        }
    }
    if assignee_gid:
        body["data"]["assignee"] = assignee_gid
    r = requests.post(ASANA_HOST + "/tasks", json=body,
                      headers=asana_headers(), timeout=60)
    r.raise_for_status()
    return r.json()["data"]["gid"]


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------

SEVERITY = {
    "ORPHAN": 0, "DISOWNED": 0, "SPLIT_FAMILY": 0, "NO_FAMILY": 1, "LONE_CHILD": 1,
    "TITLE_EMPTY": 0, "AUTOGEN_WORDING": 1, "ZERO_MM": 1, "NO_MAIN_IMAGE": 1,
    "NO_BRAND": 2, "NO_MODEL": 2, "MODEL_MISMATCH": 2, "BRAND_SPELLING": 2,
    "NO_MODEL_CODE": 2, "NO_MODEL_NAME": 2,
    "JUNK_TEXT": 2, "MODEL_CASING": 3, "NO_LENS_SIZE": 3, "TOO_LONG": 3,
    "ALL_CAPS": 3, "REPEATED_WORDS": 3, "TOKEN_CASING": 4, "OUTLIER": 4,
}


def task_name(code, label, subject):
    """Deterministic name — this string IS the dedupe key. Do not change format."""
    return f"[{code}] {label} — {subject}"


FINDING_CODE_RE = re.compile(r"^\[([A-Z_]+)\]")


def finding_code(name):
    """The code is already in the task name: "[CODE] label — subject"."""
    m = FINDING_CODE_RE.match(name)
    return m.group(1) if m else "UNKNOWN"


def summarize_by_code(findings, fresh_names):
    """Per-code counts: everything found, and how much of it is new this run."""
    totals, new = Counter(), Counter()
    for _sev, name, _notes, _units in findings:
        code = finding_code(name)
        totals[code] += 1
        if name in fresh_names:
            new[code] += 1
    return totals, new


def write_findings_csv(findings, fresh_names, path=FINDINGS_CSV):
    """Every finding, not just the capped set.

    Also echoed to stdout: the Render container's disk does not outlive the
    run, so the log is the only copy you can actually retrieve.
    """
    rows = [("code", "severity", "units_at_risk", "is_new", "name", "notes")]
    for sev, name, notes, units in sorted(findings, key=lambda f: (f[0], -f[3])):
        rows.append((
            finding_code(name), sev, units,
            "yes" if name in fresh_names else "no",
            name,
            " | ".join(l.strip() for l in notes.splitlines() if l.strip()),
        ))
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    text = buf.getvalue()

    try:
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        log(f"wrote {len(rows) - 1} findings to {path}")
    except OSError as e:
        log(f"could not write {path} ({e}) — logging the CSV only")

    print(f"----- BEGIN {path} -----", flush=True)
    print(text, end="", flush=True)
    print(f"----- END {path} -----", flush=True)


def load_ignore_list():
    try:
        with open("ignore.txt") as f:
            return {l.strip() for l in f if l.strip() and not l.startswith("#")}
    except FileNotFoundError:
        return set()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    require_env("LWA_CLIENT_ID", "LWA_CLIENT_SECRET", "LWA_REFRESH_TOKEN", "ASANA_PAT")
    log(f"starting — DRY_RUN={DRY_RUN}, ALLOW_EMPTY_BOARD={ALLOW_EMPTY_BOARD}")

    # ---- Asana state first: if the board is unreadable, don't waste 15 min on reports
    existing, suppressed = read_board_state()
    log(f"asana state: {len(existing)} existing task names, {len(suppressed)} suppressed")
    ignore = load_ignore_list()

    token = lwa_token()

    # ---- Reports
    listings_id = request_report(token, LISTINGS_REPORT)
    inv_id = request_report(token, INVENTORY_REPORT)
    listings_doc = wait_for_report(token, listings_id, "all-listings")
    inv_doc = wait_for_report(token, inv_id, "fba-inventory", fatal=False)
    if inv_doc is None:
        inv_doc = recover_inventory_report(token)

    listings = parse_tsv(download_report(token, listings_doc))
    inventory = parse_tsv(download_report(token, inv_doc))
    log(f"parsed {len(listings)} listings rows, {len(inventory)} inventory rows")

    # fulfillable units per SKU
    units_by_sku = {}
    for row in inventory:
        sku = col(row, "sku", "seller-sku")
        qty = col(row, "afn-fulfillable-quantity", "quantity available")
        try:
            units_by_sku[sku] = int(float(qty or 0))
        except ValueError:
            units_by_sku[sku] = 0

    # active listings with ASINs
    sku_to_asin, asin_units = {}, defaultdict(int)
    asin_to_skus = defaultdict(set)
    for row in listings:
        sku = col(row, "seller-sku", "sku")
        asin = col(row, "asin1", "asin", "product-id")
        status = col(row, "status").lower()
        if not asin:
            continue
        if status and "inactive" in status:
            continue
        sku_to_asin[sku] = asin
        asin_to_skus[asin].add(sku)
        asin_units[asin] += units_by_sku.get(sku, 0)

    active_asins = {a for a, u in asin_units.items() if u > 0}
    log(f"{len(active_asins)} ASINs with active FBA inventory")
    if not active_asins:
        die("zero ASINs with inventory — refusing to continue (bad report?)")

    # ---- Parent mapping
    cat = map_parents(token, active_asins)

    families = defaultdict(list)          # parent -> [child asins]
    orphans = []                          # active child with no parent at all
    for asin in sorted(active_asins):
        info = cat.get(asin)
        if not info:
            continue
        if info["parent"]:
            families[info["parent"]].append(asin)
        else:
            orphans.append(asin)

    # fetch parent details (title, image) for title QA
    parent_info = map_parents(token, families.keys()) if families else {}

    findings = []  # (severity, name, notes, units_at_risk)

    # ---- A) variation integrity
    for asin in orphans:
        skus = asin_to_skus.get(asin, set())
        if skus and all(ORPHAN_EXCLUDE_SKU_RE.search(s) for s in skus):
            continue  # bundle SKUs are intentionally parentless — never orphans
        name = task_name("ORPHAN", "ASIN has no parent", asin)
        notes = (f"https://www.amazon.com/dp/{asin}\n\n"
                 f"This ASIN has active FBA inventory ({asin_units[asin]} units) but is not "
                 f"attached to any parent. Check if it fell off its family or was never twisted in.")
        findings.append((SEVERITY["ORPHAN"], name, notes, asin_units[asin]))

    # group families by model code to spot splits
    # Keyed by (model code, category): a model's sunglasses and its replacement
    # lenses share a code but are genuinely different products and must never
    # be merged. (Aug 20 2026 finding — OO9188, OO9208, OO9295.)
    code_to_parents = defaultdict(set)
    for parent, kids in families.items():
        p_title = (parent_info.get(parent) or {}).get("title", "")
        category = product_category(p_title)
        codes = {c.upper() for c in MODEL_CODE_RE.findall(p_title)}
        for kid in kids:
            k_title = (cat.get(kid) or {}).get("title", "")
            codes.update(c.upper() for c in MODEL_CODE_RE.findall(k_title))
        for c in codes:
            code_to_parents[(c, category)].add(parent)

    for (code, category), parents in sorted(code_to_parents.items()):
        if len(parents) > 1:
            plist = ", ".join(sorted(parents))
            subject = code if category == "SUN" else f"{code} ({category.lower()})"
            name = task_name("SPLIT_FAMILY", f"Model {code} split across {len(parents)} parents", subject)
            units = sum(asin_units[k] for p in parents for k in families[p])
            notes = (f"Model {code} appears under multiple parents: {plist}\n\n"
                     f"Children of one model should live under a single parent. "
                     f"Merge the families or confirm these are genuinely different products.")
            findings.append((SEVERITY["SPLIT_FAMILY"], name, notes, units))

    for parent, kids in families.items():
        if len(kids) == 1:
            kid = kids[0]
            name = task_name("LONE_CHILD", "Parent has only one active child", parent)
            notes = (f"Parent https://www.amazon.com/dp/{parent} has a single active child "
                     f"{kid} ({asin_units[kid]} units). Check whether siblings fell off the family.")
            findings.append((SEVERITY["LONE_CHILD"], name, notes, asin_units[kid]))

    # ---- B) parent title QA
    all_parent_titles = [(p, (parent_info.get(p) or {}).get("title", "")) for p in families]
    sig_counts = Counter(structure_signature(t) for _, t in all_parent_titles if t)
    total_parents = max(sum(sig_counts.values()), 1)

    for parent, kids in families.items():
        info = parent_info.get(parent) or {}
        p_title = info.get("title", "")
        child_codes = []
        for kid in kids:
            child_codes.extend(MODEL_CODE_RE.findall((cat.get(kid) or {}).get("title", "")))
        units = sum(asin_units[k] for k in kids)

        for code, label in title_findings(p_title, child_model_codes=child_codes,
                                          has_image=info.get("has_image", True)):
            name = task_name(code, label, parent)
            notes = (f"Parent: https://www.amazon.com/dp/{parent}\n"
                     f"Title: {p_title or '(empty)'}\n"
                     f"Children with inventory: {len(kids)} ({units} units)\n\n"
                     f"Issue: {label}")
            findings.append((SEVERITY.get(code, 3), name, notes, units))

        if p_title:
            sig = structure_signature(p_title)
            if sig_counts[sig] / total_parents < 0.02 and total_parents >= 50:
                name = task_name("OUTLIER", "Title structure is a catalog outlier", parent)
                notes = (f"Parent: https://www.amazon.com/dp/{parent}\n"
                         f"Title: {p_title}\n\n"
                         f"This title's structure appears in under 2% of the catalog — "
                         f"eyeball it against the standard pattern.")
                findings.append((SEVERITY["OUTLIER"], name, notes, units))

    # ---- Dedupe, suppress, cap, create
    fresh = [(sev, name, notes, units) for sev, name, notes, units in findings
             if name not in existing and name not in suppressed and name not in ignore]
    fresh.sort(key=lambda f: (f[0], -f[3]))

    log(f"{len(findings)} findings total, {len(fresh)} new after dedupe/suppression")
    to_create = fresh[:MAX_TASKS_PER_RUN]
    deferred = len(fresh) - len(to_create)
    if deferred > 0:
        log(f"capping at {MAX_TASKS_PER_RUN}; {deferred} deferred to tomorrow's run")

    if DRY_RUN:
        fresh_names = {name for _sev, name, _notes, _units in fresh}
        totals, new_by_code = summarize_by_code(findings, fresh_names)
        log("DRY_RUN — no tasks created.")
        log(f"findings by code ({len(findings)} total, {len(fresh)} new):")
        for code, n in totals.most_common():
            log(f"  {code}: {n} ({new_by_code[code]} new)")
        write_findings_csv(findings, fresh_names)
        log(f"top {len(to_create)} that WOULD be created:")
        for sev, name, notes, units in to_create:
            log(f"  sev{sev} units={units}  {name}")
        return

    assignee = find_assignee_gid()
    for sev, name, notes, units in to_create:
        gid = create_task(name, notes, assignee)
        log(f"created task {gid}: {name}")
        time.sleep(0.4)

    log("done")


if __name__ == "__main__":
    main()
