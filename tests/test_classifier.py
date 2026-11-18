"""Classifier tests use a fake model, so they run without an API key or network."""
from recon.classifier import classify, clean


def fake(answer):
    return lambda complaint, temperature: answer


def test_valid_output_passes_through():
    r = classify("paid 200 to friend not received", call=fake({"dispute_type": "P2P failed", "confidence": 0.9}))
    assert r["dispute_type"] == "P2P failed" and r["confidence"] == 0.9


def test_unknown_label_becomes_needs_review():
    assert clean({"dispute_type": "Refund please", "confidence": 0.99})["dispute_type"] == "Needs review"


def test_confidence_is_clamped_and_parsed():
    assert clean({"dispute_type": "P2P failed", "confidence": "1.7"})["confidence"] == 1.0
    assert clean({"dispute_type": "P2P failed", "confidence": "high"})["confidence"] == 0.0


def test_model_error_becomes_needs_review():
    def broken(complaint, temperature):
        raise TimeoutError("no response")
    r = classify("anything", call=broken)
    assert r["dispute_type"] == "Needs review" and "classifier error" in r["reason"]


def test_code_guard_catches_fraud_the_model_missed():
    r = classify("someone called and asked my OTP, then 5000 was gone",
                 call=fake({"dispute_type": "P2P failed", "confidence": 0.9, "fraud_signal": False}))
    assert r["fraud_signal"] is True


def test_disagreeing_runs_force_needs_review():
    answers = iter([{"dispute_type": "P2P failed", "confidence": 0.9},
                    {"dispute_type": "P2M failed", "confidence": 0.9},
                    {"dispute_type": "P2P failed", "confidence": 0.9}])
    r = classify("paid seller 400", runs=3, call=lambda c, temperature: next(answers))
    assert r["dispute_type"] == "Needs review" and r["agreement"] == 2 / 3
