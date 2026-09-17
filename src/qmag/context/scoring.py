"""Headline / message scoring.

Default scorer: VADER (fast, no model download) with a finance lexicon layered
on top - VADER thinks "beats" is violent and "short" is a length, so the
domain words that actually move a momentum stock (guidance raise, FDA
approval, offering, downgrade, ...) are scored explicitly. The same pass
tags catalysts so the plan can say *why* a stock gapped.

Optional: ``pip install qmag[finbert]`` and set ``QMAG_SCORER=finbert`` to
use ProsusAI/finBERT (https://github.com/ProsusAI/finBERT) for headline tone;
catalyst tags still come from the lexicon.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache

# phrase -> (score contribution, catalyst tag)
FINANCE_LEXICON: dict[str, tuple[float, str | None]] = {
    # earnings / guidance
    "beats": (0.6, "earnings"), "beat estimates": (0.7, "earnings"), "tops estimates": (0.6, "earnings"),
    "record revenue": (0.7, "earnings"), "record quarter": (0.6, "earnings"), "blowout": (0.7, "earnings"),
    "raises guidance": (0.9, "guidance"), "raised guidance": (0.9, "guidance"), "raises outlook": (0.8, "guidance"),
    "boosts outlook": (0.7, "guidance"), "above expectations": (0.6, "earnings"), "strong demand": (0.5, None),
    "misses": (-0.6, "earnings"), "missed estimates": (-0.7, "earnings"), "falls short": (-0.5, "earnings"),
    "cuts guidance": (-0.9, "guidance"), "lowers guidance": (-0.9, "guidance"), "lowers outlook": (-0.8, "guidance"),
    "withdraws guidance": (-0.9, "guidance"), "weak demand": (-0.5, None), "profit warning": (-0.9, "guidance"),
    "earnings": (0.0, "earnings"), "quarterly results": (0.0, "earnings"), "q1 results": (0.0, "earnings"),
    "q2 results": (0.0, "earnings"), "q3 results": (0.0, "earnings"), "q4 results": (0.0, "earnings"),
    # deals / corporate
    "acquire": (0.3, "m&a"), "acquisition": (0.3, "m&a"), "to be acquired": (0.8, "m&a"), "takeover": (0.5, "m&a"),
    "merger": (0.2, "m&a"), "buyout": (0.6, "m&a"), "strategic review": (0.3, "m&a"),
    "contract": (0.4, "contract"), "awarded": (0.5, "contract"), "partnership": (0.4, "partnership"),
    "collaboration": (0.3, "partnership"), "wins": (0.4, "contract"), "order": (0.2, "contract"),
    "buyback": (0.5, "buyback"), "share repurchase": (0.5, "buyback"), "dividend increase": (0.3, None),
    # biotech / regulatory
    "fda approval": (0.9, "fda"), "approves": (0.7, "fda"), "approved": (0.6, "fda"), "breakthrough": (0.5, "fda"),
    "positive topline": (0.9, "trial"), "positive results": (0.7, "trial"), "met primary endpoint": (0.9, "trial"),
    "phase 3": (0.2, "trial"), "phase 2": (0.1, "trial"), "fast track": (0.4, "fda"), "priority review": (0.4, "fda"),
    "complete response letter": (-0.9, "fda"), "crl": (-0.8, "fda"), "failed to meet": (-0.9, "trial"),
    "clinical hold": (-0.9, "trial"), "did not meet": (-0.9, "trial"), "discontinue": (-0.6, "trial"),
    # dilution / balance sheet
    "offering": (-0.7, "offering"), "public offering": (-0.8, "offering"), "private placement": (-0.6, "offering"),
    "direct offering": (-0.8, "offering"), "convertible notes": (-0.5, "offering"), "at-the-market": (-0.5, "offering"),
    "dilution": (-0.6, "offering"), "reverse split": (-0.6, "split"), "stock split": (0.4, "split"),
    "going concern": (-0.9, "distress"), "bankruptcy": (-1.0, "distress"), "chapter 11": (-1.0, "distress"),
    "default": (-0.7, "distress"), "restructuring": (-0.3, "distress"),
    # analysts
    "upgrade": (0.6, "analyst"), "upgrades": (0.6, "analyst"), "initiated with buy": (0.5, "analyst"),
    "price target raised": (0.5, "analyst"), "raises price target": (0.5, "analyst"), "outperform": (0.3, "analyst"),
    "downgrade": (-0.6, "analyst"), "downgrades": (-0.6, "analyst"), "price target cut": (-0.5, "analyst"),
    "lowers price target": (-0.5, "analyst"), "underperform": (-0.4, "analyst"), "sell rating": (-0.5, "analyst"),
    # legal / governance
    "investigation": (-0.5, "legal"), "lawsuit": (-0.4, "legal"), "class action": (-0.6, "legal"),
    "sec charges": (-0.9, "legal"), "fraud": (-0.9, "legal"), "subpoena": (-0.6, "legal"), "recall": (-0.6, "legal"),
    "ceo resigns": (-0.5, "management"), "cfo resigns": (-0.6, "management"), "steps down": (-0.3, "management"),
    "appoints": (0.1, "management"), "insider buying": (0.5, "insider"), "insider selling": (-0.3, "insider"),
    # momentum / market structure
    "short squeeze": (0.4, "squeeze"), "all-time high": (0.4, None), "52-week high": (0.4, None),
    "soars": (0.5, None), "surges": (0.5, None), "jumps": (0.4, None), "rallies": (0.4, None), "skyrockets": (0.5, None),
    "plunges": (-0.6, None), "tumbles": (-0.5, None), "sinks": (-0.5, None), "crashes": (-0.7, None), "slides": (-0.3, None),
    "halted": (-0.3, None), "index inclusion": (0.5, "index"), "added to s&p": (0.6, "index"), "joins s&p": (0.6, "index"),
    "russell": (0.1, "index"), "conference": (0.0, "conference"), "investor day": (0.1, "conference"),
    # macro-ish
    "tariff": (-0.3, "macro"), "sanctions": (-0.3, "macro"), "rate cut": (0.2, "macro"), "rate hike": (-0.2, "macro"),
}

_PATTERNS = [(re.compile(r"\b" + re.escape(k) + r"\b", re.I), v) for k, v in FINANCE_LEXICON.items()]

# StockTwits-style shorthand and emoji-ish tokens
SOCIAL_LEXICON: dict[str, float] = {
    "moon": 0.6, "mooning": 0.7, "rocket": 0.6, "🚀": 0.6, "🔥": 0.4, "💎": 0.3, "diamond hands": 0.4, "yolo": 0.3,
    "calls": 0.3, "long": 0.3, "bullish": 0.7, "breakout": 0.5, "ath": 0.4, "squeeze": 0.5, "buying": 0.3, "added": 0.3,
    "loading": 0.4, "ripping": 0.6, "running": 0.4, "gap up": 0.4, "beat": 0.4,
    "puts": -0.4, "short": -0.3, "shorting": -0.5, "bearish": -0.7, "dump": -0.6, "dumping": -0.6, "bag holder": -0.6,
    "bagholder": -0.6, "rug": -0.6, "scam": -0.8, "pump and dump": -0.8, "dilution": -0.6, "offering": -0.6,
    "sell": -0.3, "selling": -0.3, "crash": -0.6, "tank": -0.5, "tanking": -0.6, "dead": -0.5, "trap": -0.5, "🐻": -0.5,
    "📉": -0.5, "📈": 0.4, "overextended": -0.3, "parabolic": -0.1, "top": -0.2,
}


@lru_cache(maxsize=1)
def _vader():
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

    return SentimentIntensityAnalyzer()


@lru_cache(maxsize=1)
def _finbert():  # pragma: no cover - optional heavy dependency
    from transformers import pipeline

    return pipeline("text-classification", model="ProsusAI/finbert", top_k=None)


def scorer_name() -> str:
    return os.environ.get("QMAG_SCORER", "vader").lower()


def lexicon_hits(text: str) -> tuple[float, list[str]]:
    total, tags = 0.0, []
    for pat, (score, tag) in _PATTERNS:
        if pat.search(text):
            total += score
            if tag and tag not in tags:
                tags.append(tag)
    return total, tags


def score_headline(text: str) -> tuple[float, list[str]]:
    """Score one headline in -1..1 and return its catalyst tags."""
    text = " ".join(text.split())
    if not text:
        return 0.0, []
    lex, tags = lexicon_hits(text)
    if scorer_name() == "finbert":
        try:  # pragma: no cover
            out = _finbert()(text[:512])[0]
            probs = {o["label"]: o["score"] for o in out}
            base = probs.get("positive", 0) - probs.get("negative", 0)
        except Exception:
            base = _vader().polarity_scores(text)["compound"]
    else:
        base = _vader().polarity_scores(text)["compound"]
    score = 0.5 * base + 0.5 * max(-1.0, min(1.0, lex))
    if lex == 0 and not tags:
        score = base * 0.7  # plain-language headline: trust VADER but damp it
    return max(-1.0, min(1.0, score)), tags


def score_social(text: str, label: str | None = None) -> float:
    """Score a social post. An explicit Bullish/Bearish label from the platform dominates."""
    if label:
        lab = label.lower()
        if lab.startswith("bull"):
            return 0.8
        if lab.startswith("bear"):
            return -0.8
    low = " ".join(text.lower().split())
    lex = sum(v for k, v in SOCIAL_LEXICON.items() if k in low)
    base = _vader().polarity_scores(low)["compound"]
    return max(-1.0, min(1.0, 0.5 * base + 0.5 * max(-1.0, min(1.0, lex))))
