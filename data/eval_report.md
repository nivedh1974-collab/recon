# Recon classifier evaluation (holdout2)

Run 24 Nov 2026 11:38, model `openai/gpt-oss-120b`, 33 complaints.

**Overall accuracy: 85%** (pass mark 90%)

## Per dispute type

| Type | Precision | Recall | Complaints |
| --- | --- | --- | --- |
| P2P failed | 100% | 100% | 1 |
| P2M failed | 100% | 88% | 8 |
| Wrong UPI ID | 50% | 100% | 1 |
| Out of scope | 95% | 83% | 23 |
| Needs review | 0% | n/a | 0 |

## Per language

| Language | Accuracy | Complaints | Note |
| --- | --- | --- | --- |
| en | 84% | 32 |  |
| hi | n/a | 0 | too few to judge (under 10) |
| hinglish | 100% | 1 | too few to judge (under 10) |

## Confusion matrix (rows = expected, columns = predicted)

| Expected \ Predicted | P2P failed | P2M failed | Wrong UPI ID | Out of scope | Needs review |
| --- | --- | --- | --- | --- | --- |
| P2P failed | 1 | 0 | 0 | 0 | 0 |
| P2M failed | 0 | 7 | 0 | 1 | 0 |
| Wrong UPI ID | 0 | 0 | 1 | 0 | 0 |
| Out of scope | 0 | 0 | 1 | 19 | 3 |
| Needs review | 0 | 0 | 0 | 0 | 0 |

Wrong labels with confidence below 0.8: 3 of 5.

Possible-fraud flag: caught 18 of 20 fraud complaints; false alarms on 1 of 13 others.
