# Recon

A copilot for the support agent at a UPI app (PhonePe, Google Pay, Paytm). It reads a failed-payment complaint, checks the transaction status, applies RBI's refund deadlines and compensation rules, drafts a reply, and flags cases that must go to the payment service provider bank. The agent approves and sends every message. Recon never moves money.

Independent portfolio build. Transaction data is simulated; the test set is 61 real complaints from r/UPI.

## Status

- [x] PRD, AI behaviour spec, data dictionary
- [x] Mock payment system: 500 UPI transactions + status API
- [ ] Dispute classifier and reply drafter (model)
- [x] Rules engine and escalation (code)
- [ ] Agent screens
- [ ] Evaluation on 61 real complaints

## Run it

```bash
pip install -r requirements.txt
python -m recon.seed            # builds data/recon.db with 500 transactions
uvicorn recon.api:app --reload  # status API at http://127.0.0.1:8000/docs
python -m pytest
```

## Mock data

| What | Based on |
| --- | --- |
| App mix: PhonePe 53%, Google Pay 38%, Paytm 9% | NPCI UPI Applications, Sep 2026 |
| Payer banks and debit-reversal success rates | NPCI Top 50 Remitter Banks, Aug 2026 |
| Error codes and meanings | NPCI UPI Error and Response Codes v2.9 |

The failure rate is deliberately enriched (about 30% problem cases vs under 1% in real UPI) so every Recon path gets tested.

## Layout

```
recon/db.py           schema (5 tables) and RBI rules config
recon/error_codes.py  NPCI response codes used in the mock data
recon/seed.py         generates the 500 mock transactions
recon/api.py          transaction status API (FastAPI)
recon/rules_engine.py RBI deadline, compensation and escalation (no model)
tests/                API tests
```
