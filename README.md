# Recon

A copilot for the support agent at a UPI app (PhonePe, Google Pay, Paytm). It reads a failed-payment complaint, checks the transaction status, applies RBI's refund deadlines and compensation rules, drafts a reply, and flags cases that must go to the payment service provider bank. The agent approves and sends every message. Recon never moves money.

Independent portfolio build. Transaction data is simulated; the test set is 61 real complaints from r/UPI.

## Status

- [x] PRD, AI behaviour spec, data dictionary
- [x] Mock payment system: 500 UPI transactions + status API
- [x] API v1: status lookup, classify, draft reply, PSP escalation (spec: docs/api.md)
- [x] Failure handling: one status retry, then Needs review with the reason shown
- [x] Audit log: full case record at open, append-only history, case history page at /cases/{id}
- [x] Possible-fraud flag: route to fraud team, money path held, agent confirms or clears
- [x] Dispute classifier (model)
- [ ] Reply drafter (model)
- [x] Rules engine and escalation (code)
- [x] Ops metrics dashboard (/dashboard, /metrics)
- [ ] Agent screens
- [ ] Evaluation on 61 real complaints

## Run it

```bash
pip install -r requirements.txt
python -m recon.seed            # builds data/recon.db with 500 transactions
python -m recon.simulate         # optional: simulated agent sessions for the dashboard
uvicorn recon.api:app --reload  # API docs at /docs, ops dashboard at /dashboard
python -m pytest
```

### Classifier and evaluation

Needs a free [Groq](https://console.groq.com/keys) API key.

```bash
export GROQ_API_KEY=your_key
# Download the complaints sheet as CSV to data/complaints_test_set.csv, then:
python -m recon.evaluate            # one run per complaint
python -m recon.evaluate --runs 3   # with the agreement check
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
recon/api_v1.py       API v1 endpoints (see docs/api.md)
recon/drafter.py      template reply drafter (never sends)
recon/case_flow.py    opens a case end to end, applies failure rules
recon/payment_system.py  mock payment system client, one retry
recon/case_store.py   case record + append-only audit log
recon/rules_engine.py RBI deadline, compensation and escalation (no model)
recon/classifier.py   reads the complaint, returns dispute type + fields (model)
recon/evaluate.py     scores the classifier on the 61 real complaints
recon/metrics.py      ops metrics computed from cases + audit_log
recon/simulate.py     simulated agent sessions (clearly labelled) for the dashboard
tests/                API tests
```
