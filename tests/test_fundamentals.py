import pandas as pd

from qmag.config import StrategyConfig
from qmag.fundamentals import INDUSTRY_PREFIX, facts_for, fetch_fundamentals, industry_themes, load_fundamentals, save_fundamentals
from qmag.themes import load_themes


def _fake_fetch(sym: str) -> dict | None:
    table = {
        "AAA": ("Technology", "Semiconductors", 5e9, 40e6, 3.0),
        "BBB": ("Technology", "Semiconductors", 2e9, 20e6, 12.0),
        "CCC": ("Technology", "Semiconductors", 1e9, 10e6, 25.0),
        "DDD": ("Technology", "Semiconductors", 9e9, 90e6, 1.0),
        "EEE": ("Healthcare", "Biotechnology", 4e8, 8e6, 30.0),
        "FFF": ("Healthcare", "Biotechnology", 6e8, 9e6, 18.0),
    }
    if sym == "ZZZ":
        raise RuntimeError("HTTP 429 too many requests")
    if sym not in table:
        return None
    sec, ind, cap, flt, sf = table[sym]
    return {"symbol": sym, "sector": sec, "industry": ind, "market_cap": cap, "float_shares": flt, "short_float_pct": sf, "earnings": "", "updated": "2025-01-01"}


def test_fetch_save_load_roundtrip_and_merge(tmp_path):
    df = fetch_fundamentals(["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "NOPE", "ZZZ"], workers=2, pause=0, retries=2, fetch=_fake_fetch)
    assert sorted(df["symbol"]) == ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]
    path = save_fundamentals(df, tmp_path / "f.csv")
    # A later partial refresh keeps the rows it could not re-fetch.
    later = fetch_fundamentals(["AAA"], workers=1, pause=0, fetch=_fake_fetch)
    save_fundamentals(later, path)
    loaded = load_fundamentals(path)
    assert len(loaded) == 6 and loaded.loc[loaded["symbol"] == "EEE", "short_float_pct"].item() == 30.0
    assert facts_for(loaded, "ccc")["float_shares"] == 10e6
    assert facts_for(loaded, "NOPE") == {}


def test_industry_themes_respect_min_members_and_symbol_scope():
    df = fetch_fundamentals(["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"], workers=1, pause=0, fetch=_fake_fetch)
    groups = industry_themes(df, min_members=4)
    assert list(groups) == [f"{INDUSTRY_PREFIX}Semiconductors"]
    assert industry_themes(df, min_members=2) == {
        f"{INDUSTRY_PREFIX}Biotechnology": ["EEE", "FFF"],
        f"{INDUSTRY_PREFIX}Semiconductors": ["AAA", "BBB", "CCC", "DDD"],
    }
    assert industry_themes(df, symbols=["AAA", "BBB"], min_members=2) == {f"{INDUSTRY_PREFIX}Semiconductors": ["AAA", "BBB"]}


def test_load_themes_merges_industries_under_curated(monkeypatch, tmp_path):
    df = fetch_fundamentals(["AAA", "BBB", "CCC", "DDD"], workers=1, pause=0, fetch=_fake_fetch)
    path = save_fundamentals(df, tmp_path / "f.csv", merge_existing=False)
    monkeypatch.setattr("qmag.fundamentals.FUNDAMENTALS_FILE", path)
    cfg = StrategyConfig().with_overrides({"themes.groups": {"Chips": ["AAA", "XYZ"]}, "themes.min_industry_members": 3})
    themes = load_themes(cfg)
    assert themes["Chips"] == ["AAA", "XYZ"]
    assert themes[f"{INDUSTRY_PREFIX}Semiconductors"] == ["AAA", "BBB", "CCC", "DDD"]
    off = load_themes(cfg.with_overrides({"themes.use_industries": False}))
    assert list(off) == ["Chips"]
