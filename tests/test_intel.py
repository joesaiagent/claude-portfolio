"""Offline tests for the bounded news/analyst enrichment tilt."""
from agents import _intel
from agents._intel import NEWS_TILT_CAP, enrich


class _MP:
    def __init__(self): self._undo = []
    def setattr(self, obj, name, val):
        self._undo.append((obj, name, getattr(obj, name)))
        setattr(obj, name, val)
    def undo(self):
        for obj, name, val in reversed(self._undo):
            setattr(obj, name, val)
        self._undo.clear()


def _patch(mp, sent, n, upside, rec, sector="", fin=0.0):
    mp.setattr(_intel, "news_sentiment", lambda t: (sent, n))
    mp.setattr(_intel, "fundamentals", lambda t: (upside, rec, sector))
    mp.setattr(_intel, "finnhub_rec", lambda t: fin)
    mp.setattr(_intel, "finnhub_insider_sentiment", lambda t: 0.0)
    mp.setattr(_intel.time, "sleep", lambda *_: None)  # no real delay in tests


def test_tilt_capped_positive(mp):
    _patch(mp, sent=1.0, n=50, upside=100.0, rec=1.0)  # maximally bullish
    c = enrich([{"ticker": "AAA", "score": 50.0}])[0]
    assert c["momentum_score"] == 50.0                  # momentum preserved
    assert c["score"] == round(50.0 + NEWS_TILT_CAP, 2)  # tilt clamped to +12
    for k in ("news_sentiment", "news_count", "analyst_upside_pct", "analyst_rec", "conviction", "thesis"):
        assert k in c


def test_tilt_capped_negative(mp):
    # sent -1 (-6) + upside -50 (-7.5) + rec 5 (-4) = -17.5 -> clamped to -12.
    _patch(mp, sent=-1.0, n=50, upside=-50.0, rec=5.0)
    c = enrich([{"ticker": "BBB", "score": 50.0}])[0]
    assert c["score"] == round(50.0 - NEWS_TILT_CAP, 2)


def test_tilt_modest_when_mild(mp):
    _patch(mp, sent=0.1, n=10, upside=5.0, rec=3.0)
    c = enrich([{"ticker": "CCC", "score": 50.0}])[0]
    tilt = c["score"] - 50.0
    assert 0 < tilt < NEWS_TILT_CAP                     # small, not clamped


def test_healthcare_flagged_excluded(mp):
    _patch(mp, sent=0.2, n=20, upside=10.0, rec=2.0, sector="Healthcare")
    c = enrich([{"ticker": "LLY", "score": 60.0}])[0]
    assert c["excluded"] is True
    # A tech-sector name is not excluded.
    _patch(mp, sent=0.2, n=20, upside=10.0, rec=2.0, sector="Technology")
    c2 = enrich([{"ticker": "NVDA", "score": 60.0}])[0]
    assert c2["excluded"] is False


def run():
    for fn in (test_tilt_capped_positive, test_tilt_capped_negative,
               test_tilt_modest_when_mild, test_healthcare_flagged_excluded):
        mp = _MP()
        try:
            fn(mp)
        finally:
            mp.undo()
