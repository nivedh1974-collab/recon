from recon.evaluate import expected_label, report_md, score, split_of
from recon.language import detect


def r(expected, predicted, lang="en", conf=0.9):
    return {"expected": expected, "predicted": predicted, "correct": expected == predicted,
            "language": lang, "confidence": conf}


def test_precision_recall_and_confusion():
    s = score([r("P2P failed", "P2P failed"), r("P2P failed", "P2M failed"),
               r("P2M failed", "P2M failed"), r("Wrong UPI ID", "P2P failed", conf=0.5)])
    assert s["accuracy"] == 0.5
    assert s["per_type"]["P2P failed"] == {"precision": 0.5, "recall": 0.5, "support": 2}
    assert s["per_type"]["P2M failed"] == {"precision": 0.5, "recall": 1.0, "support": 1}
    assert s["confusion"]["P2P failed"] == {"P2P failed": 1, "P2M failed": 1}
    assert s["wrong"] == 2 and s["wrong_caught_by_confidence"] == 1


def test_small_language_groups_are_flagged():
    s = score([r("P2P failed", "P2P failed", lang="hinglish")])
    assert s["per_language"]["hinglish"] == {"n": 1, "accuracy": 1.0, "enough": False}
    assert "too few to judge" in report_md(s, "holdout", "test-model")


def test_language_detection():
    assert detect("मेरा पैसा कट गया") == "hi"
    assert detect("bhai mera paisa kat gaya par mila nahi") == "hinglish"
    assert detect("Money debited but receiver did not get it") == "en"


def test_split_defaults():
    assert split_of({"ID": "C014"}) == "dev"
    assert split_of({"ID": "C120"}) == "holdout"
    assert split_of({"ID": "C014", "Split": "holdout"}) == "holdout"
    assert expected_label("Out of scope (fraud)") == "Out of scope"
