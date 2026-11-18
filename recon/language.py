"""Tag a complaint as en, hi (Devanagari) or hinglish (Hindi in Latin script).

A simple rule, good enough to split eval results. Tags can be overridden in the
test set's Language column when a person disagrees.
"""
import re

DEVANAGARI = re.compile(r"[ऀ-ॿ]")
# Common Hindi words written in Latin script. Two or more hits = Hinglish.
HINGLISH_WORDS = {
    "hai", "hain", "nahi", "nahin", "nhi", "kya", "kaise", "mera", "meri", "mere", "maine", "mujhe", "paisa",
    "paise", "kat", "kata", "gaya", "gayi", "gaye", "aaya", "aayi", "hua", "hui", "raha", "rahi", "bhai",
    "kar", "karo", "kiya", "diya", "liya", "wapas", "vaapis", "abhi", "tak", "bhi", "aur", "lekin", "par",
    "se", "ko", "ka", "ki", "ke", "de", "do", "ho", "hoga", "sahi", "galat", "bata", "batao", "jaldi",
}


def detect(text: str) -> str:
    if DEVANAGARI.search(text or ""):
        return "hi"
    words = re.findall(r"[a-z]+", (text or "").lower())
    hits = sum(1 for w in words if w in HINGLISH_WORDS)
    return "hinglish" if hits >= 2 else "en"
