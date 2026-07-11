"""
Piaci-állapot (regime) osztályozó — a „market strategy" MATEK-magja.

A `Tananyagok/market-regime-classifier.md` (v2, validált: EURUSD M5/M15,
2022–2025) alapján. Három dimenzió Wilder-féle ADX/±DI + ATR-arány:

    IRÁNY   : DI_diff = +DI(n) − −DI(n)
    ERŐ     : ADX(n)   (Wilder-simítás)
    VOLATIL.: ATR_ratio = ATR(n) / SMA(ATR(n), avg_n)   (>1 = átlag feletti)

8 kategória (v2), prioritási sorrenddel az átfedések feloldására. A küszöbök
`params`-ból jönnek (alap = a validált v2 értékek), hogy az optimizer KALIBRÁLNI
tudja őket. A modul SZÁNDÉKOSAN MT5-mentes és tiszta (pandas/numpy) — a live, a
backtest és az elemző eszköz is használhatja.

FONTOS: hogy egy kategória „kereskedhető-e", az a KERESKEDŐ-stratégiától és az
instrumentumtól függ (a validáció szerint pl. az Oldalazás a mean-reversionnek
jó, a trendkövetőnek rossz). Ezt NEM itt döntjük el — ez a modul csak OBJEKTÍV
kategóriát ad; a kategória→(kihagy/óvatos/normál) leképezést az optimizer méri ki.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# Kategória-kulcsok (stabil, gépi nevek; a megjelenítés fordíthatja magyarra)
CLEAN_BULL    = "clean_bull"      # Szép Bika
CLEAN_BEAR    = "clean_bear"      # Szép Medve
VOLATILE_BULL = "volatile_bull"   # Ideges Bika
VOLATILE_BEAR = "volatile_bear"   # Ideges Medve
RANGING       = "ranging"         # Oldalazás
DEAD          = "dead"            # Érdektelenség
UNCERTAIN     = "uncertain"       # Bizonytalanság / Kaotikus
TRANSITION    = "transition"      # Átmenet
UNCATEGORIZED = "uncategorized"

CATEGORIES = (CLEAN_BULL, CLEAN_BEAR, VOLATILE_BULL, VOLATILE_BEAR,
              RANGING, DEAD, UNCERTAIN, TRANSITION, UNCATEGORIZED)

NAME_HU = {
    CLEAN_BULL: "Szép Bika", CLEAN_BEAR: "Szép Medve",
    VOLATILE_BULL: "Ideges Bika", VOLATILE_BEAR: "Ideges Medve",
    RANGING: "Oldalazás", DEAD: "Érdektelenség",
    UNCERTAIN: "Bizonytalanság", TRANSITION: "Átmenet",
    UNCATEGORIZED: "Besorolatlan",
}

# Egész KÓD a vizualizációhoz (a STATE-sor `regime` mezője + az MQL5 szín-index).
# 0 = besorolatlan/nincs. A színeket a TradeForgeBands indikátor rendeli hozzá.
CODE = {
    UNCATEGORIZED: 0, CLEAN_BULL: 1, CLEAN_BEAR: 2,
    VOLATILE_BULL: 3, VOLATILE_BEAR: 4, RANGING: 5,
    DEAD: 6, UNCERTAIN: 7, TRANSITION: 8,
}


def code(category: str) -> int:
    """Kategória → egész kód (a viz STATE-sorához és a Bands szín-indexéhez)."""
    return CODE.get(category, 0)

# A validált v2 alap-küszöbök (az optimizer felülírhatja `params`-ban).
DEFAULT_PARAMS = {
    "adx_period":     14,
    "atr_period":     14,
    "atr_avg_period": 100,
    "adx_trend":      25.0,   # e fölött „szép" (erős) trend
    "adx_weak":       20.0,   # e alatt nincs trend (range/uncertain)
    "adx_dead":       15.0,   # e alatt „érdektelen" (alvó piac)
    "di_strong":      10.0,   # |DI_diff| e fölött határozott irány
    "di_flat":        5.0,    # |DI_diff| e alatt iránytalan
    "atr_hi":         1.5,    # e fölött „ideges" (magas volatilitás)
    "atr_uncertain":  1.3,    # ADX gyenge + e fölött → Bizonytalanság (v2)
    "atr_low":        0.7,    # e alatt „alvó" piac
    "atr_clean_lo":   0.7,    # „szép" trend volatilitás-sávja
    "atr_clean_hi":   1.3,
    "atr_range_lo":   0.8,    # oldalazás volatilitás-sávja
    "atr_range_hi":   1.2,
}


def _wilder(s: pd.Series, n: int) -> pd.Series:
    """Wilder-simítás (RMA): alpha = 1/n. (A standard ADX/ATR ezt használja.)"""
    return s.ewm(alpha=1.0 / n, adjust=False).mean()


def adx_di(df: pd.DataFrame, n: int = 14):
    """Wilder ADX, +DI, −DI. Visszaad: (adx, plus_di, minus_di) Series-ek."""
    high, low, close = df["high"], df["low"], df["close"]
    up   = high.diff()
    down = -low.diff()
    plus_dm  = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(),
                    (low - prev_close).abs()], axis=1).max(axis=1)
    atr = _wilder(tr, n)
    plus_di  = 100.0 * _wilder(pd.Series(plus_dm, index=df.index), n) / atr.replace(0, np.nan)
    minus_di = 100.0 * _wilder(pd.Series(minus_dm, index=df.index), n) / atr.replace(0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = _wilder(dx.fillna(0.0), n)
    return adx, plus_di, minus_di


def atr_ratio(df: pd.DataFrame, n: int = 14, avg_n: int = 100) -> pd.Series:
    """ATR(n) / SMA(ATR(n), avg_n) — a jelenlegi volatilitás a saját alapvonalához."""
    high, low, close = df["high"], df["low"], df["close"]
    prev = close.shift(1)
    tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()],
                   axis=1).max(axis=1)
    atr = _wilder(tr, n)
    base = atr.rolling(avg_n).mean()
    return atr / base.replace(0, np.nan)


def features(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    """A regime-dimenziók idősorai: adx, di_diff, atr_ratio (egy DataFrame-ben)."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    adx, pdi, mdi = adx_di(df, int(p["adx_period"]))
    ar = atr_ratio(df, int(p["atr_period"]), int(p["atr_avg_period"]))
    return pd.DataFrame({"adx": adx, "di_diff": pdi - mdi, "atr_ratio": ar},
                        index=df.index)


def _classify_row(adx: float, di_diff: float, ar: float, p: dict) -> str:
    """Egy sor kategóriája a v2 prioritási sorrenddel (első találat nyer):
    Bizonytalanság > Érdektelenség > Ideges > Szép > Oldalazás > Átmenet."""
    if np.isnan(adx) or np.isnan(ar):
        return UNCATEGORIZED
    adi = abs(di_diff)
    # 1) Bizonytalanság (legveszélyesebb): gyenge irány + magas volatilitás
    if adx < p["adx_weak"] and ar > p["atr_uncertain"]:
        return UNCERTAIN
    # 2) Érdektelenség: alvó piac
    if adx < p["adx_dead"] and adi < p["di_flat"] and ar < p["atr_low"]:
        return DEAD
    # 3) Ideges Bika/Medve: van irány, de magas volatilitás
    if adx > p["adx_weak"] and ar > p["atr_hi"]:
        return VOLATILE_BULL if di_diff > 0 else VOLATILE_BEAR
    # 4) Szép Bika/Medve: erős irány, normál volatilitás
    if adx > p["adx_trend"] and p["atr_clean_lo"] <= ar <= p["atr_clean_hi"]:
        if di_diff > p["di_strong"]:
            return CLEAN_BULL
        if di_diff < -p["di_strong"]:
            return CLEAN_BEAR
    # 5) Oldalazás: nincs irány, normál volatilitás
    if adx < p["adx_weak"] and adi < p["di_strong"] \
            and p["atr_range_lo"] <= ar <= p["atr_range_hi"]:
        return RANGING
    # 6) Átmenet (v2): a szürke ADX-zóna
    if p["adx_dead"] <= adx <= p["adx_trend"]:
        return TRANSITION
    return UNCATEGORIZED


def classify(df: pd.DataFrame, params: dict | None = None) -> pd.Series:
    """Per-gyertya kategória (a v2 küszöbökkel). Visszaad: str Series a df indexén."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    feat = features(df, p)
    out = [
        _classify_row(a, d, r, p)
        for a, d, r in zip(feat["adx"].values, feat["di_diff"].values,
                           feat["atr_ratio"].values)
    ]
    return pd.Series(out, index=df.index, name="regime")
