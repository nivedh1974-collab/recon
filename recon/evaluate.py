"""Score the classifier against the 61 real complaints (answer key in the complaints sheet).

1. In the Google Sheet "Recon - Complaints Log": File > Download > CSV.
2. Save it as data/complaints_test_set.csv
3. Run:  python -m recon.evaluate            (add --runs 3 for the agreement check)

Writes data/eval_results.csv and prints the pass marks from the AI behaviour spec.
"""
import argparse
import csv
from pathlib import Path

from recon.classifier import classify

DATA = Path(__file__).resolve().parent.parent / "data"

# Answer key wording in the sheet -> classifier labels
KEY_TO_LABEL = {"P2P": "P2P failed", "P2M": "P2M failed", "Wrong UPI ID": "Wrong UPI ID", "Needs review": "Needs review"}


def expected_label(raw: str) -> str:
    raw = (raw or "").strip()
    if raw.startswith("Out of scope"):
        return "Out of scope"
    return KEY_TO_LABEL.get(raw, raw)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=1)
    args = parser.parse_args()

    with open(DATA / "complaints_test_set.csv", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    results, correct = [], 0
    for row in rows:
        want = expected_label(row["Expected type"])
        got = classify(row["Complaint text"], runs=args.runs)
        ok = got["dispute_type"] == want
        correct += ok
        results.append({
            "id": row["ID"], "expected": want, "predicted": got["dispute_type"], "correct": ok,
            "confidence": got["confidence"], "fraud_signal": got["fraud_signal"], "reason": got["reason"],
        })
        print(f"{row['ID']}  {'OK ' if ok else 'MISS'}  want={want:<14} got={got['dispute_type']:<14} conf={got['confidence']:.2f}")

    with open(DATA / "eval_results.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(results[0]))
        writer.writeheader()
        writer.writerows(results)

    accuracy = correct / len(rows)
    wrong = [r for r in results if not r["correct"]]
    caught = sum(1 for r in wrong if r["confidence"] < 0.8)
    print(f"\nDispute type accuracy: {correct}/{len(rows)} = {accuracy:.0%}  (pass mark 90%)")
    print(f"Wrong labels flagged by confidence < 0.8: {caught}/{len(wrong)}  (tells us if 0.8 is the right cut-off)")
    print("PASS" if accuracy >= 0.9 else "FAIL: fix prompt/examples before building screens")


if __name__ == "__main__":
    main()
