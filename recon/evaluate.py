"""Score the classifier against real complaints (answer key in the complaints sheet).

    python -m recon.evaluate                  # all rows
    python -m recon.evaluate --split holdout  # holdout only (the real score)
    python -m recon.evaluate --split dev
    python -m recon.evaluate --runs 3         # agreement check (uses 3x the Groq budget)

Test set: data/complaints_test_set.csv. Optional columns:
    Split     dev | holdout   (default: C001-C065 = dev, everything else = holdout)
    Language  en | hi | hinglish   (default: detected by recon.language)

Writes data/eval_results.csv and data/eval_report.md.
"""
import argparse
import csv
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from recon.language import detect

DATA = Path(__file__).resolve().parent.parent / "data"
LABELS = ["P2P failed", "P2M failed", "Wrong UPI ID", "Out of scope", "Needs review"]
LANGS = ["en", "hi", "hinglish"]
MIN_FOR_JUDGEMENT = 10

KEY_TO_LABEL = {"P2P": "P2P failed", "P2M": "P2M failed", "Wrong UPI ID": "Wrong UPI ID", "Needs review": "Needs review"}


def expected_label(raw: str) -> str:
    raw = (raw or "").strip()
    if raw.startswith("Out of scope"):
        return "Out of scope"
    return KEY_TO_LABEL.get(raw, raw)


def split_of(row: dict) -> str:
    if row.get("Split"):
        return row["Split"].strip().lower()
    cid = row["ID"].strip()
    num = int(cid[1:]) if cid[1:].isdigit() else 10**6
    return "dev" if cid.startswith("C") and num <= 65 else "holdout"


def language_of(row: dict) -> str:
    return (row.get("Language") or "").strip().lower() or detect(row["Complaint text"])


def score(results: list) -> dict:
    """Pure function: precision/recall per type, accuracy per language, confusion matrix."""
    confusion = defaultdict(Counter)
    for r in results:
        confusion[r["expected"]][r["predicted"]] += 1
    per_type = {}
    for label in LABELS:
        tp = confusion[label][label]
        predicted = sum(confusion[e][label] for e in confusion)
        actual = sum(confusion[label].values())
        per_type[label] = {
            "precision": round(tp / predicted, 3) if predicted else None,
            "recall": round(tp / actual, 3) if actual else None,
            "support": actual,
        }
    per_lang = {}
    for lang in LANGS:
        rows = [r for r in results if r["language"] == lang]
        per_lang[lang] = {
            "n": len(rows),
            "accuracy": round(sum(r["correct"] for r in rows) / len(rows), 3) if rows else None,
            "enough": len(rows) >= MIN_FOR_JUDGEMENT,
        }
    wrong = [r for r in results if not r["correct"]]
    fr = [r for r in results if "expected_fraud" in r]
    fraud = {
        "fraud_complaints": sum(r["expected_fraud"] for r in fr),
        "caught": sum(r["expected_fraud"] and r["fraud_signal"] for r in fr),
        "false_alarms": sum((not r["expected_fraud"]) and r["fraud_signal"] for r in fr),
        "others": sum(not r["expected_fraud"] for r in fr),
    }
    return {
        "n": len(results),
        "accuracy": round(sum(r["correct"] for r in results) / len(results), 3) if results else None,
        "per_type": per_type,
        "per_language": per_lang,
        "confusion": {e: dict(c) for e, c in confusion.items()},
        "wrong": len(wrong),
        "wrong_caught_by_confidence": sum(1 for r in wrong if r["confidence"] < 0.8),
        "fraud_flag": fraud,
    }


def _fmt(x):
    return "n/a" if x is None else f"{x:.0%}"


def report_md(s: dict, split: str, model: str) -> str:
    lines = [f"# Recon classifier evaluation ({split})", "",
             f"Run {datetime.now():%d %b %Y %H:%M}, model `{model}`, {s['n']} complaints.", "",
             f"**Overall accuracy: {_fmt(s['accuracy'])}** (pass mark 90%)", "",
             "## Per dispute type", "", "| Type | Precision | Recall | Complaints |", "| --- | --- | --- | --- |"]
    for label, v in s["per_type"].items():
        lines.append(f"| {label} | {_fmt(v['precision'])} | {_fmt(v['recall'])} | {v['support']} |")
    lines += ["", "## Per language", "", "| Language | Accuracy | Complaints | Note |", "| --- | --- | --- | --- |"]
    for lang, v in s["per_language"].items():
        note = "" if v["enough"] else f"too few to judge (under {MIN_FOR_JUDGEMENT})"
        lines.append(f"| {lang} | {_fmt(v['accuracy'])} | {v['n']} | {note} |")
    lines += ["", "## Confusion matrix (rows = expected, columns = predicted)", "",
              "| Expected \\ Predicted | " + " | ".join(LABELS) + " |", "| --- |" + " --- |" * len(LABELS)]
    for e in LABELS:
        row = s["confusion"].get(e, {})
        lines.append(f"| {e} | " + " | ".join(str(row.get(p, 0)) for p in LABELS) + " |")
    lines += ["", f"Wrong labels with confidence below 0.8: {s['wrong_caught_by_confidence']} of {s['wrong']}."]
    f = s.get("fraud_flag") or {}
    if f.get("fraud_complaints") or f.get("others"):
        lines += ["", f"Possible-fraud flag: caught {f['caught']} of {f['fraud_complaints']} fraud complaints; "
                      f"false alarms on {f['false_alarms']} of {f['others']} others."]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--split", choices=["all", "dev", "holdout"], default="all")
    args = parser.parse_args()

    from recon.classifier import DEFAULT_MODEL, classify

    with open(DATA / "complaints_test_set.csv", newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r.get("ID") and r.get("Expected type")]
    if args.split != "all":
        rows = [r for r in rows if split_of(r) == args.split]
    if not rows:
        sys.exit(f"No rows in split '{args.split}'.")
    if not os.environ.get("GROQ_API_KEY"):
        sys.exit("GROQ_API_KEY is not set. Run: export GROQ_API_KEY=your_key")

    results = []
    for row in rows:
        want = expected_label(row["Expected type"])
        got = classify(row["Complaint text"], runs=args.runs)
        if got["reason"].startswith("classifier error"):
            sys.exit(f"Stopped at {row['ID']}: {got['reason']}")
        ok = got["dispute_type"] == want
        results.append({
            "id": row["ID"], "split": split_of(row), "language": language_of(row), "expected": want,
            "predicted": got["dispute_type"], "correct": ok, "confidence": got["confidence"],
            "fraud_signal": got["fraud_signal"], "reason": got["reason"],
            "expected_fraud": "fraud" in row["Expected type"].lower(),
            "fraud_signals": "; ".join(got.get("fraud_signals", [])),
            "overrides": "; ".join(got.get("overrides", [])),
        })
        print(f"{row['ID']}  {'OK ' if ok else 'MISS'}  want={want:<14} got={got['dispute_type']:<14} conf={got['confidence']:.2f}")

    with open(DATA / "eval_results.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(results[0]))
        writer.writeheader()
        writer.writerows(results)

    s = score(results)
    md = report_md(s, args.split, os.environ.get("GROQ_MODEL", DEFAULT_MODEL))
    (DATA / "eval_report.md").write_text(md, encoding="utf-8")
    print("\n" + md)
    print("PASS" if s["accuracy"] >= 0.9 else "FAIL: below 90%")


if __name__ == "__main__":
    main()
