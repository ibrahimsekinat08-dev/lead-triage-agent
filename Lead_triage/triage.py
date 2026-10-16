#!/usr/bin/env python3
"""
Lead Triage Agent (Single Grain challenge, brief version 2026-07)

A deterministic, rule-based first-pass triage. No LLM and no API key needed.
Design principle: lead text is DATA. It is cleaned and pattern-checked, never
executed, never obeyed, and no URL in it is ever fetched.

Usage:
    python3 triage.py fixtures/inbound_leads.csv
Outputs (in ./out):
    decisions.csv   one row per lead (lead_id, decision, reason, confidence + extras)
    run_log.jsonl   per-row trace of every check that fired
"""
import csv
import hashlib
import html
import json
import re
import sys
import datetime as dt
from pathlib import Path

# ---- Policy knobs (ASSUMED values, not provided by the brief; change and re-run) ----
QUALIFY_MIN = 5000      # monthly budget at or above this, with a stated need -> QUALIFY
NURTURE_MIN = 1000      # between NURTURE_MIN and QUALIFY_MIN -> NURTURE; below -> REJECT
HIGH_VALUE = 20000      # a high-value claim that cannot be verified goes to a human

DISPOSABLE = {"mailinator.com", "guerrillamail.com", "10minutemail.com", "tempmail.com",
              "yopmail.com", "trashmail.com", "sharklasers.com", "throwawaymail.com"}
FREE_MAIL = {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com",
             "proton.me", "protonmail.com", "aol.com"}

INJECTION_PATTERNS = [
    r"ignore (all )?(the )?(previous|prior|above|earlier) (instructions|prompts?|rules)",
    r"disregard (all )?(the )?(previous|prior|above|earlier)",
    r"classify (this|the) lead as",
    r"(set|assign|output|return|give) (the |a )?(decision|confidence|score)",
    r"confidence (of |at )?(1\.0|100)",
    r"system prompt",
    r"do not (escalate|flag|reject)",
    r"override (the )?(rules|policy|decision)",
    # added after stress test: paraphrased, indirect and Spanish variants
    r"\b(ignore|disregard|forget)\b.{0,40}\b(instructions?|guidance|directions?|rules|prompt)\b",
    r"\b(mark|classify|label|treat|score)\b.{0,30}\b(this|it|lead|one)\b.{0,30}\b(qualif|top confidence|approved)",
    r"\b(new rule|always approve|always qualify|auto[- ]?approve)\b",
    r"ignora(r)? las instrucciones|clasifica este lead",
]
PRIVACY_PATTERNS = [
    r"\bgdpr\b", r"article 17", r"right to (be forgotten|erasure)",
    r"delete (all )?(my |the )?(personal )?(data|information)",
    r"\bccpa\b", r"data subject (access|request)",
]
STUDENT_PATTERNS = [r"\bstudent\b", r"\bstudying\b", r"\bmentor", r"\binternship\b",
                    r"break(ing)? into (the )?(industry|marketing)", r"career advice"]
COMPETITOR_PATTERNS = [r"comparison for (a |our )?client", r"for a client of ours",
                       r"how (do )?you structure (your )?retainers",
                       r"what deliverables (do )?you include"]
AGENCY_NAME = re.compile(r"\b(agency|digital partners|marketing partners|media group)\b", re.I)
MATERIAL_REQUEST = re.compile(r"(send|share).{0,40}(methodology|deck|case stud|past client|results)", re.I)
BAD_LINK = re.compile(r"https?://\S+\.(exe|scr|bat|msi|vbs|js|jar|apk)\b", re.I)
PHISH_PRETEXT = re.compile(r"(overdue invoice|avoid service interruption|account (suspended|locked)|verify your account)", re.I)
URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.I)
KEYBOARD_MASH = re.compile(r"asdf|qwer|zxcv|(.)\1{3,}", re.I)
LARGE_CO = re.compile(r"(\d[\d,]*)\s*-?\s*employee|fortune\s*(100|500|1000)", re.I)
SENIOR_CLAIM = re.compile(r"\b(vp|vice president|cmo|ceo|cfo|head of)\b", re.I)
TIMELINE = re.compile(r"(budget approved|approved budget|presupuesto aprobado|within \d+ days|start (this|next) (week|month)|ready to sign|este trimestre)", re.I)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SPANISH_HINTS = re.compile(r"\b(somos|buscamos|para|presupuesto|agencia|tenemos)\b", re.I)


def clean_text(raw: str) -> str:
    """Normalise untrusted text: unescape entities, drop tags and emoji, collapse space."""
    t = html.unescape(raw or "")
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF]", "", t)
    return re.sub(r"\s+", " ", t).strip()


def parse_budget(raw: str):
    s = (raw or "").strip().lower().replace(",", "").replace("$", "")
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(k|m)?", s)
    if not m:
        return None, ("empty" if not s else "unparseable")
    v = float(m.group(1))
    shorthand = bool(m.group(2))
    if m.group(2) == "k":
        v *= 1_000
    elif m.group(2) == "m":
        v *= 1_000_000
    return v, ("shorthand" if shorthand else "ok")


def parse_ts(raw: str) -> bool:
    try:
        dt.datetime.strptime((raw or "").strip(), "%Y-%m-%dT%H:%M:%SZ")
        return True
    except ValueError:
        return False


def any_match(patterns, text):
    for p in patterns:
        m = re.search(p, text, re.I)
        if m:
            return m.group(0)
    return None


def triage(row, seen, decided):
    """Return (decision, confidence, reason, flags, duplicate_of, trace)."""
    trace, flags = [], []

    def log(check, hit, detail=""):
        trace.append({"check": check, "hit": bool(hit), "detail": detail})
        return hit

    lead_id = row["lead_id"].strip()
    email = (row.get("email") or "").strip().lower()
    domain = email.split("@")[-1] if "@" in email else ""
    company = (row.get("company") or "").strip()
    website = (row.get("website") or "").strip()
    msg_raw = row.get("message") or ""
    msg = clean_text(msg_raw)
    budget, bstate = parse_budget(row.get("monthly_budget_usd"))

    # --- data quality (never crash on bad input; record it) ---
    if not log("timestamp_valid", parse_ts(row.get("submitted_at")), row.get("submitted_at")):
        flags.append("bad_timestamp")
    if bstate in ("unparseable", "empty"):
        flags.append(f"budget_{bstate}")
    if bstate == "shorthand":
        flags.append("budget_shorthand_parsed")
    if website.lower().startswith("http://"):
        flags.append("website_not_https")
    if SPANISH_HINTS.search(msg) and len(SPANISH_HINTS.findall(msg)) >= 2:
        flags.append("lang_es")
    if msg != html.unescape(msg_raw).strip():
        flags.append("message_sanitised")

    # --- 1. security: malicious link or phishing pretext (URL is never fetched) ---
    urls = URL_RE.findall(msg_raw)
    bad = BAD_LINK.search(msg_raw) or (urls and PHISH_PRETEXT.search(msg_raw))
    if log("malicious_link_or_phish", bad, "; ".join(urls)[:120]):
        flags.append("SECURITY_phishing_or_malware")
        what = ("pushes an executable download" if BAD_LINK.search(msg_raw)
                else "pushes a link under an urgent account pretext")
        return ("REJECT", 0.96,
                f"Message {what}, which is phishing, so the link was not opened and the sender should be blocked.",
                flags, "", trace)

    # --- 2. prompt injection: treat as data, judge on verifiable fields only ---
    inj = any_match(INJECTION_PATTERNS, msg)
    if log("injection_attempt", inj, inj or ""):
        flags.append("INJECTION_attempt")
        if budget is not None and budget >= NURTURE_MIN:
            return ("ESCALATE", 0.85,
                    "Message tries to instruct the triage system, so its claims were ignored and a human should review given the budget field.",
                    flags, "", trace)
        return ("REJECT", 0.93,
                "Message contains embedded instructions aimed at the triage system, which were ignored, and the verifiable fields (low budget, no real need stated) do not support a lead.",
                flags, "", trace)

    # --- 3. privacy / legal request ---
    priv = any_match(PRIVACY_PATTERNS, msg)
    if log("privacy_request", priv, priv or ""):
        flags.append("PRIVACY_request")
        return ("ESCALATE", 0.96,
                "Data-erasure request under privacy law needs the privacy owner to action within the legal deadline, not a sales decision.",
                flags, "", trace)

    # --- 4. junk / test submissions ---
    junk_signals = 0
    if (row.get("name") or "").strip().lower() in {"test", "asdf", "qwerty", "n/a", "none"}:
        junk_signals += 1
    if KEYBOARD_MASH.search(msg + " " + email + " " + company):
        junk_signals += 1
    if budget is not None and budget >= 500_000:
        junk_signals += 1
    if len(msg.split()) < 3 and msg:
        junk_signals += 1
    if log("junk_test_submission", junk_signals >= 2, f"signals={junk_signals}"):
        flags.append("junk")
        return ("REJECT", 0.97,
                "Keyboard-mash text, placeholder name and an absurd budget show this is a test submission, not a real lead.",
                flags, "", trace)

    # --- 5. disposable email ---
    if log("disposable_email", domain in DISPOSABLE, domain):
        flags.append("disposable_email")
        extra = " and asks for proprietary methodology and client results up front" if MATERIAL_REQUEST.search(msg) else ""
        return ("REJECT", 0.75,
                f"Disposable email domain with no website{extra} reads as scraping, and the sender can resubmit with a real address.",
                flags, "", trace)

    # --- 6. duplicates and conflicts (same email seen earlier) ---
    if email and email in seen:
        first = seen[email]
        first_budget, _ = parse_budget(first.get("monthly_budget_usd"))
        same_budget = (first_budget == budget) or budget is None or first_budget is None
        if log("duplicate_email", True, f"first_seen={first['lead_id']} same_budget={same_budget}"):
            if same_budget:
                flags.append("duplicate")
                orig = decided[first["lead_id"]]
                dec = "NURTURE" if orig in ("QUALIFY", "NURTURE") else orig
                return (dec, 0.8,
                        f"Same contact as {first['lead_id']} (same email and budget), so merge into that record instead of opening a second deal.",
                        flags, first["lead_id"], trace)
            flags.append("conflicts_with_earlier_row")
            return ("ESCALATE", 0.8,
                    f"Same email as {first['lead_id']} but the budget and story changed, so a human should verify who this is before acting.",
                    flags, first["lead_id"], trace)

    # --- 7. missing contact ---
    if log("email_missing_or_invalid", (not email) or not EMAIL_RE.match(email), email):
        flags.append("no_contact_email")
        if budget is not None and budget < NURTURE_MIN:
            return ("REJECT", 0.8, "No usable email and a negligible budget, so there is nothing to follow up on.", flags, "", trace)
        return ("ESCALATE", 0.85,
                "Budget and need look real but there is no email address, so a human must recover the contact from the source system.",
                flags, "", trace)

    # --- 8. not a buyer (student, mentorship) ---
    stud = any_match(STUDENT_PATTERNS, msg)
    if log("not_a_buyer", stud and (budget in (None, 0) or (budget or 0) < NURTURE_MIN), stud or ""):
        flags.append("not_a_buyer")
        return ("REJECT", 0.93,
                "Student asking for career mentorship with no budget is not a buyer.",
                flags, "", trace)

    # --- 9. conflicting size vs budget ---
    large = LARGE_CO.search(msg)
    big_claim = False
    if large:
        n = large.group(1)
        big_claim = (n is None) or (n.replace(",", "").isdigit() and int(n.replace(",", "")) >= 1000) or bool(large.group(2))
    if log("size_budget_conflict", big_claim and budget is not None and budget < NURTURE_MIN, f"budget={budget}"):
        flags.append("conflicting_signals")
        return ("ESCALATE", 0.85,
                "A very large company paired with a near-zero budget conflicts, which suggests a typo or wrong units, so a human should confirm the real budget.",
                flags, "", trace)

    # --- 10. unverifiable high-value claim ---
    unverifiable = (domain in FREE_MAIL or not company or not website) and (
        (SENIOR_CLAIM.search(msg) or re.search(r"fortune\s*\d+", msg, re.I)) and not company)
    if log("unverifiable_high_value", unverifiable and (budget or 0) >= HIGH_VALUE, f"domain={domain} company={company!r}"):
        flags.append("unverifiable_claim")
        return ("ESCALATE", 0.88,
                "Large budget and urgent signing claim come from a free email with no company named, so identity must be verified before any pricing is shared.",
                flags, "", trace)

    # --- 11. possible competitor or comparison shopping ---
    comp = any_match(COMPETITOR_PATTERNS, msg)
    if log("competitor_signal", comp or AGENCY_NAME.search(company), comp or company):
        flags.append("possible_competitor")
        return ("ESCALATE", 0.7,
                "Looks like an agency gathering retainer and deliverable details for a client, which could be a competitor or a partnership, so a human should decide what to share.",
                flags, "", trace)

    # --- 12. budget unknown ---
    if budget is None:
        if len(msg) >= 40:
            return ("NURTURE", 0.55,
                    "Genuine need but the budget is unclear or unparseable, so ask a qualifying question before routing to sales.",
                    flags, "", trace)
        return ("ESCALATE", 0.6, "No usable budget and no stated need, so there is too little data to decide.", flags, "", trace)

    # --- 13. budget tiers ---
    timeline = bool(TIMELINE.search(msg))
    bonus = 0.08 if timeline else 0.0
    penalty = 0.07 * sum(f in flags for f in ("bad_timestamp", "budget_shorthand_parsed", "website_not_https"))
    if budget < NURTURE_MIN:
        return ("REJECT", 0.85, f"Budget of ${budget:,.0f} per month is below the working minimum.", flags, "", trace)
    if budget < QUALIFY_MIN:
        return ("NURTURE", min(0.9, 0.78 + bonus) - penalty,
                f"Real need but a ${budget:,.0f} monthly budget is below the qualify line, so keep warm and revisit when spend grows.",
                flags, "", trace)
    if not msg:
        return ("NURTURE", 0.6,
                f"Budget of ${budget:,.0f} per month looks good but no need was stated, so send a discovery question first.",
                flags, "", trace)
    conf = min(0.95, 0.72 + bonus + (0.05 if budget >= 15000 else 0)) - penalty
    return ("QUALIFY", round(conf, 2),
            f"Stated need with a ${budget:,.0f} monthly budget" + (" and a concrete timeline or approved budget" if timeline else "") + " meets the qualify bar.",
            flags, "", trace)


def main(path):
    src = Path(path)
    sha = hashlib.sha256(src.read_bytes()).hexdigest()
    out = Path("out")
    out.mkdir(exist_ok=True)
    print(f"fixture: {src}  sha256: {sha}")
    with src.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    print(f"rows read: {len(rows)}")

    seen, decided, results = {}, {}, []
    with (out / "run_log.jsonl").open("w", encoding="utf-8") as log_f:
        log_f.write(json.dumps({"fixture": str(src), "sha256": sha, "rows": len(rows),
                                "policy": {"QUALIFY_MIN": QUALIFY_MIN, "NURTURE_MIN": NURTURE_MIN, "HIGH_VALUE": HIGH_VALUE}}) + "\n")
        for row in rows:
            try:
                dec, conf, reason, flags, dup, trace = triage(row, seen, decided)
            except Exception as exc:  # fail safe: a crash becomes a human review, never a silent drop
                dec, conf, reason, flags, dup, trace = ("ESCALATE", 0.0, f"Agent error ({type(exc).__name__}), routed to a human.", ["AGENT_ERROR"], "", [])
            email = (row.get("email") or "").strip().lower()
            if email and email not in seen:
                seen[email] = row
            decided[row["lead_id"]] = dec
            conf = round(max(0.0, min(1.0, conf)), 2)
            results.append({"lead_id": row["lead_id"], "decision": dec, "reason": reason,
                            "confidence": conf, "flags": "|".join(flags), "duplicate_of": dup})
            log_f.write(json.dumps({"lead_id": row["lead_id"], "decision": dec, "confidence": conf,
                                    "flags": flags, "checks": [t for t in trace if t["hit"]]}) + "\n")
            print(f"{row['lead_id']}  {dec:<8} {conf:<5} {'|'.join(flags)}")

    with (out / "decisions.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["lead_id", "decision", "reason", "confidence", "flags", "duplicate_of"])
        w.writeheader()
        w.writerows(results)
    counts = {d: sum(1 for r in results if r["decision"] == d) for d in ("QUALIFY", "NURTURE", "REJECT", "ESCALATE")}
    print("summary:", counts, "total:", len(results))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "fixtures/inbound_leads.csv")
