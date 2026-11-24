"""Dispute classifier: the model's first job (see AI behaviour spec).

Reads a complaint in English, Hindi or Hinglish and returns structured fields.
It does NOT decide deadlines, compensation or escalation; that is rules_engine.py.

Needs a Groq API key in the environment:
    export GROQ_API_KEY=...        (or put it in a .env file, which git ignores)
Optional:
    export GROQ_MODEL=openai/gpt-oss-120b
"""
import json
import os
import re
import time
from collections import Counter
from typing import Optional

import httpx

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_MODEL = "openai/gpt-oss-120b"
CLASSIFIER_TIMEOUT_S = 20  # per Groq call; assumption, see PRD API spec

DISPUTE_TYPES = ["P2P failed", "P2M failed", "Wrong UPI ID", "Out of scope", "Needs review"]
OUT_OF_SCOPE = ["AutoPay", "fraud", "double payment", "merchant display issue", "cards", "wallet"]

# Code-level fraud guard. Runs even if the model misses it.
# Possible-fraud signals (PRD: Possible fraud). Built from known UPI fraud types, not from the
# holdout. Plain "fraud" is NOT a signal: angry customers call the app a fraud all the time.
FRAUD_SIGNALS = {
    "unauthorised debit": [
        r"(didn'?t|did not|never|not) (make|do|initiate|authori[sz]e)[a-z]* (this |the |that |any |these )?"
        r"(payment|transaction|transfer|debit)",
        r"without my (knowledge|permission|consent|approval)", r"\bunauthori[sz]ed\b"],
    "OTP or PIN shared": [r"\botp\b", r"\b(upi )?pin\b.{0,40}\b(share|shared|asked|gave|told)\b",
                          r"\b(share|shared|asked|gave|told)\b.{0,40}\bupi pin\b"],
    "remote access app": [r"anydesk|teamviewer|quick ?support|screen ?shar|remote access"],
    "fake customer care": [r"customer care (number )?(from|on) google",
                           r"fake (customer care|call|link|app|website|number|agent)"],
    "collect or QR trick": [r"(scan|scanned)\b.{0,30}\breceive", r"collect request"],
    "scam words": [r"\bscam", r"\bcheat(ed|ing)?\b", r"\bfraudster", r"\bfraudulent\b", r"\bhack(ed|er|ing)?\b",
                   r"\bfraud (call|caller|link|message|sms|happened|attempt|case|person|with me)",
                   r"(victim of|been|got) (a )?fraud", r"\bdefraud", r"\bthag", r"dhokha",
                   r"fraud (hua|ho gaya|kiya)", r"(unknown|strange) caller"],
    "job, task or investment scam": [r"part[- ]time job", r"task (job|based)", r"\btelegram\b",
                                     r"investment (app|scheme|plan)", r"double (your|the) money", r"\blottery\b",
                                     r"prize money"],
    "crypto deal": [r"\bcrypto\b", r"\busdt\b", r"\bbitcoin\b"],
}
FRAUD_PATTERNS = [p for pats in FRAUD_SIGNALS.values() for p in pats]

SYSTEM_PROMPT = """You classify UPI payment complaints for a support agent at a UPI app in India.
Complaints may be in English, Hindi or Hinglish. Return ONLY a JSON object, no prose.

dispute_type, pick exactly one:
- "P2P failed": paid a person; money debited but the receiver did not get it, or status stuck pending.
- "P2M failed": paid a shop, merchant, biller, recharge, website or payment gateway; money debited but
  the merchant shows failed / not received, or the order or recharge did not happen.
- "Wrong UPI ID": the customer paid the wrong person, wrong number, wrong QR or same-name person by mistake.
- "Out of scope": AutoPay/mandates, suspected fraud or scam, a payment that went through twice to the
  right person (double payment), a merchant-side display or settlement issue, cards, or wallets.
- "Needs review": the text does not say what went wrong at all. Do not guess.

Tie-breakers (apply in order):
1. Wrong person beats everything: if the customer paid the wrong person or wrong QR, it is "Wrong UPI ID"
   even if they paid twice.
2. "Double payment" (Out of scope) ONLY when the intended payee actually received both payments.
   If one payment is stuck, pending, failed or not received, it is a failed payment (P2P or P2M), not a
   double payment.
3. Paying an individual for a crypto trade, investment, task, job or online deal that never delivered is
   "Out of scope" with out_of_scope_reason "fraud".
4. A UPI ID on another app (name@paytm, number@ybl) is a person unless the text names a business.
5. If money was debited, the payment failed or the receiver didn't get it, and the text does not name a
   shop, merchant, biller or website, treat it as "P2P failed". Use "Needs review" only when even that
   much is missing.
6. These are "Out of scope", never P2M: adding money to the customer's own wallet or app balance
   (out_of_scope_reason "wallet"); a payment funded by a credit card (cards); a refund the merchant owes
   for a cancelled order, ticket or booking (merchant display issue); a recurring AutoPay debit (AutoPay).
7. If the customer also paid the same person another way (cash, another app) and is now out of pocket
   twice, it is "Out of scope" with out_of_scope_reason "double payment".

Rules:
- Never invent facts. Use null for anything the complaint does not state.
- amount_inr is a number in rupees, or null. If two payments are mentioned, give the total.
- confidence is your honest probability (0 to 1) that dispute_type is correct.
- fraud_signal is true if the customer mentions not authorising the payment, a call asking for OTP or
  PIN, a scam, or a stranger asking them to pay or "receive" money.

JSON keys: dispute_type, confidence, amount_inr, txn_date, utr, app, fraud_signal, fraud_words,
out_of_scope_reason (one of AutoPay, fraud, double payment, merchant display issue, cards, wallet, or null),
reason (one short sentence)."""

# Synthetic examples, written for this prompt. None are from the 61-complaint test set.
FEW_SHOT = [
    ("Bhai maine 1500 bheje dost ko gpay se, paisa kat gaya par usko nahi mila. 2 din ho gaye.",
     {"dispute_type": "P2P failed", "confidence": 0.95, "amount_inr": 1500, "txn_date": None, "utr": None,
      "app": "Google Pay", "fraud_signal": False, "fraud_words": [], "out_of_scope_reason": None,
      "reason": "Paid a friend, debited, not received."}),
    ("Paid 499 for Airtel recharge on PhonePe, money gone from SBI but recharge not done.",
     {"dispute_type": "P2M failed", "confidence": 0.95, "amount_inr": 499, "txn_date": None, "utr": None,
      "app": "PhonePe", "fraud_signal": False, "fraud_words": [], "out_of_scope_reason": None,
      "reason": "Recharge to a merchant failed after debit."}),
    ("I sent 3000 to the wrong number, typed one digit wrong. How do I get it back?",
     {"dispute_type": "Wrong UPI ID", "confidence": 0.97, "amount_inr": 3000, "txn_date": None, "utr": None,
      "app": None, "fraud_signal": False, "fraud_words": [], "out_of_scope_reason": None,
      "reason": "Customer paid the wrong recipient by mistake."}),
    ("Someone called saying I won a prize and asked me to enter my UPI PIN to receive it. 10000 gone.",
     {"dispute_type": "Out of scope", "confidence": 0.95, "amount_inr": 10000, "txn_date": None, "utr": None,
      "app": None, "fraud_signal": True, "fraud_words": ["called", "UPI PIN"], "out_of_scope_reason": "fraud",
      "reason": "Caller tricked customer into entering PIN."}),
    ("Tried to top up my Paytm wallet with 2000, bank debited but wallet balance still zero.",
     {"dispute_type": "Out of scope", "confidence": 0.9, "amount_inr": 2000, "txn_date": None, "utr": None,
      "app": "Paytm", "fraud_signal": False, "fraud_words": [], "out_of_scope_reason": "wallet",
      "reason": "Wallet top-up, not a UPI payment to someone else."}),
    ("Flight got cancelled, airline says refund done, but the booking app still has not returned my 6400.",
     {"dispute_type": "Out of scope", "confidence": 0.85, "amount_inr": 6400, "txn_date": None, "utr": None,
      "app": None, "fraud_signal": False, "fraud_words": [], "out_of_scope_reason": "merchant display issue",
      "reason": "Merchant refund for a cancelled booking, not a failed payment."}),
    ("Paid rent 15000 using my credit card on the app, landlord says not received.",
     {"dispute_type": "Out of scope", "confidence": 0.85, "amount_inr": 15000, "txn_date": None, "utr": None,
      "app": None, "fraud_signal": False, "fraud_words": [], "out_of_scope_reason": "cards",
      "reason": "Card-funded payment; card network rules apply, not UPI."}),
    ("paisa kat gaya",
     {"dispute_type": "Needs review", "confidence": 0.3, "amount_inr": None, "txn_date": None, "utr": None,
      "app": None, "fraud_signal": False, "fraud_words": [], "out_of_scope_reason": None,
      "reason": "Says money was debited but not to whom, how much or when."}),
    ("money stuck help",
     {"dispute_type": "Needs review", "confidence": 0.3, "amount_inr": None, "txn_date": None, "utr": None,
      "app": None, "fraud_signal": False, "fraud_words": [], "out_of_scope_reason": None,
      "reason": "Does not say who was paid or what happened."}),
]


def fraud_guard(text: str) -> list:
    """Names of the fraud signals found in the text, e.g. ["OTP or PIN shared"]."""
    lowered = text.lower()
    return [name for name, pats in FRAUD_SIGNALS.items() if any(re.search(p, lowered) for p in pats)]


def _messages(complaint: str) -> list:
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
    for text, answer in FEW_SHOT:
        msgs.append({"role": "user", "content": text})
        msgs.append({"role": "assistant", "content": json.dumps(answer)})
    msgs.append({"role": "user", "content": complaint})
    return msgs


def prompt_version() -> str:
    """Short hash of the prompt and examples, stored on every case so a label can be traced to its prompt."""
    import hashlib
    return hashlib.sha256((SYSTEM_PROMPT + json.dumps(FEW_SHOT, sort_keys=True)).encode()).hexdigest()[:12]


def _call_model(complaint: str, temperature: float) -> dict:
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        raise RuntimeError("GROQ_API_KEY is not set")
    payload = {
        "model": os.environ.get("GROQ_MODEL", DEFAULT_MODEL),
        "messages": _messages(complaint),
        "temperature": temperature,
        "response_format": {"type": "json_object"},
    }
    # Free tier allows ~8K tokens/minute, so expect 429s. Wait and retry.
    for attempt in range(6):
        resp = httpx.post(GROQ_URL, headers={"Authorization": f"Bearer {key}"}, json=payload, timeout=CLASSIFIER_TIMEOUT_S)
        if resp.status_code != 429:
            break
        wait = float(resp.headers.get("retry-after", 15))
        print(f"  rate limited, waiting {wait:.0f}s")
        time.sleep(wait)
    if resp.status_code >= 400:
        raise RuntimeError(f"Groq {resp.status_code}: {resp.text[:300]}")
    return json.loads(resp.json()["choices"][0]["message"]["content"])


def clean(raw: dict) -> dict:
    """Force the model output into the spec. Anything malformed becomes Needs review."""
    out = {
        "dispute_type": raw.get("dispute_type"),
        "confidence": raw.get("confidence"),
        "amount_inr": raw.get("amount_inr"),
        "txn_date": raw.get("txn_date"),
        "utr": raw.get("utr"),
        "app": raw.get("app"),
        "fraud_signal": bool(raw.get("fraud_signal", False)),
        "fraud_words": raw.get("fraud_words") or [],
        "out_of_scope_reason": raw.get("out_of_scope_reason"),
        "reason": raw.get("reason", ""),
    }
    problems = []
    if out["dispute_type"] not in DISPUTE_TYPES:
        problems.append(f"dispute_type {out['dispute_type']!r} is not allowed")
        out["dispute_type"], out["confidence"] = "Needs review", 0.0
    try:
        out["confidence"] = max(0.0, min(1.0, float(out["confidence"])))
    except (TypeError, ValueError):
        problems.append(f"confidence {out['confidence']!r} is not a number")
        out["confidence"] = 0.0
    if out["out_of_scope_reason"] not in OUT_OF_SCOPE:
        out["out_of_scope_reason"] = None
    if not isinstance(out["amount_inr"], (int, float)):
        out["amount_inr"] = None
    out["failure"] = "schema" if problems else None
    out["schema_problems"] = problems
    return out


# Fix 1 (holdout run, 19 Nov 2026): vague complaints were guessed as P2P/P2M.
AMOUNT_RE = re.compile(r"(₹|\brs\.?|\binr\b|rupees?|\brs\b)\s*\d|\d[\d,]{1,}")
DATE_RE = re.compile(
    r"\d{1,2}[/-]\d{1,2}|\b\d{1,2}(st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
    r"|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d|yesterday|today|\bkal\b|\baaj\b"
    r"|\d+(\.\d+)?\s*(hours?|hrs?|days?|din)\b")
PAYEE_RE = re.compile(
    r"\b(friends?|dost|mother|mom|father|dad|sister|brother|bhai|wife|husband|relatives?|person|someone"
    r"|landlord|receiver|payee|beneficiary|shop|shopkeeper|store|merchant|seller|dukan|recharged?|bills?"
    r"|electricity|tickets?|booking|order|amazon|flipkart|swiggy|zomato|jio|airtel|vi|bsnl|irctc|petrol"
    r"|fees?|rent|challan|gateway|payu|razorpay|upi id|number|qr|own[- ]account)\b|@")
STATUS_RE = re.compile(r"\b(failed|declined|pending|processing|credited)\b")


def lacks_detail(complaint: str) -> bool:
    """True when the complaint names no amount, payee, date or payment status."""
    t = complaint.lower()
    return not any(r.search(t) for r in (AMOUNT_RE, DATE_RE, PAYEE_RE, STATUS_RE))


# Fix 3: the linked transaction beats the model's guess.
FUNDING_OUT_OF_SCOPE = {"WALLET": "wallet", "CARD": "cards", "MANDATE": "AutoPay"}


def apply_overrides(result: dict, complaint: str, txn: Optional[dict] = None) -> dict:
    """Code rules that run after the model. Each one leaves a note in `overrides`."""
    notes = []
    if txn:
        src = (txn.get("funding_source") or "UPI").upper()
        if src in FUNDING_OUT_OF_SCOPE and result["dispute_type"] != "Out of scope":
            notes.append(f"transaction is {src}: out of scope (model said {result['dispute_type']})")
            result["dispute_type"], result["out_of_scope_reason"] = "Out of scope", FUNDING_OUT_OF_SCOPE[src]
        elif result["dispute_type"] in ("P2P failed", "P2M failed"):
            want = f"{txn['txn_type']} failed"
            if want != result["dispute_type"]:
                notes.append(f"transaction is {txn['txn_type']}: {want} (model said {result['dispute_type']})")
                result["dispute_type"] = want
    elif result["dispute_type"] in ("P2P failed", "P2M failed") and lacks_detail(complaint):
        notes.append(f"no amount, payee or date and no linked transaction (model said {result['dispute_type']})")
        result["dispute_type"] = "Needs review"
        result["confidence"] = min(result["confidence"], 0.5)
    result["overrides"] = notes
    return result


def classify(complaint: str, runs: int = 1, call=None, txn: Optional[dict] = None) -> dict:
    """Classify one complaint.

    runs > 1 turns on the agreement check from the spec: the complaint is classified
    several times and any disagreement on dispute_type sends it to Needs review.
    `call` lets tests swap in a fake model.
    """
    call = call or _call_model
    results = []
    for i in range(runs):
        try:
            results.append(clean(call(complaint, temperature=0.0 if runs == 1 else 0.7)))
        except Exception as err:  # network, bad JSON, missing key
            r = clean({"dispute_type": "Needs review", "confidence": 0.0,
                       "reason": f"classifier error: {type(err).__name__}: {err}"})
            r["failure"] = "model_timeout" if isinstance(err, httpx.TimeoutException) else "model_error"
            results.append(r)

    labels = Counter(r["dispute_type"] for r in results)
    top_label, votes = labels.most_common(1)[0]
    result = next(r for r in results if r["dispute_type"] == top_label)
    result["agreement"] = votes / runs
    if runs > 1 and votes < runs:
        result["reason"] = f"Runs disagreed: {dict(labels)}. " + result["reason"]
        result["dispute_type"] = "Needs review"
        result["confidence"] = min(result["confidence"], votes / runs)

    if not result["reason"].startswith("classifier error"):
        apply_overrides(result, complaint, txn)

    result["model_fraud_flag"] = bool(result["fraud_signal"])
    guard_hits = fraud_guard(complaint)
    if guard_hits and not result["fraud_signal"]:
        result["fraud_signal"] = True
        result["fraud_words"] = result["fraud_words"] + ["(code guard)"]
    result["fraud_signals"] = guard_hits
    return result
