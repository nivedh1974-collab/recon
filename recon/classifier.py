"""Dispute classifier: the model's first job (see AI behaviour spec).

Reads a complaint in English, Hindi or Hinglish and returns structured fields.
It does NOT decide deadlines, compensation or escalation; that is rules_engine.py.

Needs a Groq API key in the environment:
    export GROQ_API_KEY=...        (or put it in a .env file, which git ignores)
Optional:
    export GROQ_MODEL=llama-3.3-70b-versatile
"""
import json
import os
import re
from collections import Counter
from typing import Optional

import httpx

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_MODEL = "llama-3.3-70b-versatile"

DISPUTE_TYPES = ["P2P failed", "P2M failed", "Wrong UPI ID", "Out of scope", "Needs review"]
OUT_OF_SCOPE = ["AutoPay", "fraud", "double payment", "merchant display issue", "cards", "wallet"]

# Code-level fraud guard. Runs even if the model misses it.
FRAUD_PATTERNS = [
    r"\botp\b", r"\bupi pin\b", r"\bpin\b.*(ask|share|call)", r"(didn'?t|did not|never) (make|authori[sz]e)",
    r"\bscam", r"\bfraud", r"\bhack", r"unknown (call|caller|number)", r"\bstranger\b", r"remote access",
]

SYSTEM_PROMPT = """You classify UPI payment complaints for a support agent at a UPI app in India.
Complaints may be in English, Hindi or Hinglish. Return ONLY a JSON object, no prose.

dispute_type, pick exactly one:
- "P2P failed": paid a person; money debited but the receiver did not get it, or status stuck pending.
- "P2M failed": paid a shop, merchant, biller, recharge, website or payment gateway; money debited but
  the merchant shows failed / not received, or the order or recharge did not happen.
- "Wrong UPI ID": the customer paid the wrong person, wrong number, wrong QR or same-name person by mistake.
- "Out of scope": AutoPay/mandates, suspected fraud or scam, a payment that went through twice to the
  right person (double payment), a merchant-side display or settlement issue, cards, or wallets.
- "Needs review": the text does not say who was paid or what went wrong. Do not guess.

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
    ("money stuck help",
     {"dispute_type": "Needs review", "confidence": 0.3, "amount_inr": None, "txn_date": None, "utr": None,
      "app": None, "fraud_signal": False, "fraud_words": [], "out_of_scope_reason": None,
      "reason": "Does not say who was paid or what happened."}),
]


def fraud_guard(text: str) -> list:
    lowered = text.lower()
    return [p for p in FRAUD_PATTERNS if re.search(p, lowered)]


def _messages(complaint: str) -> list:
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
    for text, answer in FEW_SHOT:
        msgs.append({"role": "user", "content": text})
        msgs.append({"role": "assistant", "content": json.dumps(answer)})
    msgs.append({"role": "user", "content": complaint})
    return msgs


def _call_model(complaint: str, temperature: float) -> dict:
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        raise RuntimeError("GROQ_API_KEY is not set")
    resp = httpx.post(
        GROQ_URL,
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model": os.environ.get("GROQ_MODEL", DEFAULT_MODEL),
            "messages": _messages(complaint),
            "temperature": temperature,
            "response_format": {"type": "json_object"},
        },
        timeout=30,
    )
    resp.raise_for_status()
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
    if out["dispute_type"] not in DISPUTE_TYPES:
        out["dispute_type"], out["confidence"] = "Needs review", 0.0
    try:
        out["confidence"] = max(0.0, min(1.0, float(out["confidence"])))
    except (TypeError, ValueError):
        out["confidence"] = 0.0
    if out["out_of_scope_reason"] not in OUT_OF_SCOPE:
        out["out_of_scope_reason"] = None
    if not isinstance(out["amount_inr"], (int, float)):
        out["amount_inr"] = None
    return out


def classify(complaint: str, runs: int = 1, call=None) -> dict:
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
            results.append(clean({"dispute_type": "Needs review", "reason": f"classifier error: {err}"}))

    labels = Counter(r["dispute_type"] for r in results)
    top_label, votes = labels.most_common(1)[0]
    result = next(r for r in results if r["dispute_type"] == top_label)
    result["agreement"] = votes / runs
    if runs > 1 and votes < runs:
        result["reason"] = f"Runs disagreed: {dict(labels)}. " + result["reason"]
        result["dispute_type"] = "Needs review"
        result["confidence"] = min(result["confidence"], votes / runs)

    guard_hits = fraud_guard(complaint)
    if guard_hits and not result["fraud_signal"]:
        result["fraud_signal"] = True
        result["fraud_words"] = result["fraud_words"] + ["(code guard)"]
    return result
