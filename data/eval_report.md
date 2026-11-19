# Recon classifier evaluation (holdout)

Run 19 Nov 2026 09:54, model `openai/gpt-oss-120b`, 106 complaints.

**Overall accuracy: 78%** (pass mark 90%)

## Per dispute type

| Type | Precision | Recall | Complaints |
| --- | --- | --- | --- |
| P2P failed | 67% | 100% | 14 |
| P2M failed | 69% | 100% | 18 |
| Wrong UPI ID | 94% | 89% | 18 |
| Out of scope | 90% | 61% | 28 |
| Needs review | 78% | 64% | 28 |

## Per language

| Language | Accuracy | Complaints | Note |
| --- | --- | --- | --- |
| en | 79% | 92 |  |
| hi | 100% | 1 | too few to judge (under 10) |
| hinglish | 69% | 13 |  |

## Confusion matrix (rows = expected, columns = predicted)

| Expected \ Predicted | P2P failed | P2M failed | Wrong UPI ID | Out of scope | Needs review |
| --- | --- | --- | --- | --- | --- |
| P2P failed | 14 | 0 | 0 | 0 | 0 |
| P2M failed | 0 | 18 | 0 | 0 | 0 |
| Wrong UPI ID | 1 | 0 | 16 | 1 | 0 |
| Out of scope | 1 | 5 | 0 | 17 | 5 |
| Needs review | 5 | 3 | 1 | 1 | 18 |

Wrong labels with confidence below 0.8: 10 of 23.
