"""
ML feature-motor — a Trading-with-AI projekt feature-készletének portja.

EGYETLEN igazságforrás a tanításhoz, a backtesthez ÉS az élő jelzéshez: mindhárom
a `build_feature_frame()`-et hívja, így a modell pontosan azt látja élőben, amin
tanult. A modul tkinter/MT5-mentes (tisztán pandas/numpy), mint a strategy réteg.

Eltérés a forrástól (Trading-with-ai/ml_backtest.py):
  • A H1 kontextus NEM külön adatcsatorna, hanem az M15 gyertyákból resample-elt
    H1 — így nem kell új letöltő/parquet réteg, és a live/backtest garantáltan
    ugyanabból az adatból számol.
  • A H1 feature-öket EGY H1 gyertyával eltoljuk (shift), hogy CSAK lezárt H1
    vödörből származzanak → nincs look-ahead (a forrás backtestje a teljes-órás
    H1 sort ffill-elte, ami az óra közepén enyhe jövőbelátás volt).
  • A pip-normalizálás paraméter (`pip_size`), nem globális PAIRS lookup.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Indikátor-primitívek (a forrással bitazonos matek)
# ---------------------------------------------------------------------------

def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def rsi(s: pd.Series, n: int = 14) -> pd.Series:
    d = s.diff()
    g = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    l = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + g / l.replace(0, np.nan))


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    hl  = df["high"] - df["low"]
    hpc = (df["high"] - df["close"].shift()).abs()
    lpc = (df["low"]  - df["close"].shift()).abs()
    return pd.concat([hl, hpc, lpc], axis=1).max(axis=1).ewm(span=n, adjust=False).mean()


def williams_r(df: pd.DataFrame, n: int = 55) -> pd.Series:
    hh = df["high"].rolling(n).max()
    ll = df["low"].rolling(n).min()
    return -100 * (hh - df["close"]) / (hh - ll).replace(0, np.nan)


def wma(s: pd.Series, n: int) -> pd.Series:
    w = np.arange(1, n + 1, dtype=float)
    return s.rolling(n).apply(lambda x: np.dot(x, w) / w.sum(), raw=True)


# ---------------------------------------------------------------------------
# SMC (LiquiditySMC) — piaci struktúra, EQH/EQL, sweep
# ---------------------------------------------------------------------------

def compute_smc(df: pd.DataFrame, pip_size: float,
                swing_len: int = 5,
                eq_tol: float = 0.001,     # 0.1% tolerancia EQH/EQL-hez
                sweep_ratio: float = 0.50,
                sweep_memory: int = 4) -> pd.DataFrame:
    """A LiquiditySMC.mq5 Python-portja:
      smc_bias       : struktúra-irány (-1 bearish / 0 semleges / 1 bullish)
      smc_eqh_dist   : ár→EQH táv pipben (0, ha nincs aktív EQH)
      smc_eql_dist   : EQL→ár táv pipben (0, ha nincs aktív EQL)
      smc_near_eqh/l : az ár 5 pipen belül van az EQH/EQL-hez (0/1)
      smc_sweep_bull/bear : sweep aktív az utolsó `sweep_memory` gyertyában (0/1)
      smc_hh / smc_hl: az utolsó pivot high HH / pivot low HL volt-e (0/1)
    Előfeltétel: `atr14` oszlop már számolva.

    KAUZÁLIS eltérés a forrástól: egy pivot csak `swing_len` gyertyával KÉSŐBB
    erősödik meg (addig nem tudható, hogy tényleg szélsőérték volt-e), ezért a
    hatása CSAK a megerősítő gyertyától él. A forrás a pivot SAJÁT soránál írta
    be (backtest: 5 gyertya look-ahead; live: az utolsó 5 sor csupa nulla →
    tanítás/live eltérés). A kauzális változatban tanítás = backtest = live."""
    n_bars = len(df)
    hi    = df["high"].values
    lo    = df["low"].values
    cl    = df["close"].values
    atr_v = df["atr14"].values

    # ── 1. Pivot high/low azonosítás (swing_len gyertya mindkét oldalon) ──────
    ph_idx: set = set()
    pl_idx: set = set()
    for i in range(swing_len, n_bars - swing_len):
        v = hi[i]
        if all(hi[i - k] < v for k in range(1, swing_len + 1)) and \
           all(hi[i + k] < v for k in range(1, swing_len + 1)):
            ph_idx.add(i)
        v = lo[i]
        if all(lo[i - k] > v for k in range(1, swing_len + 1)) and \
           all(lo[i + k] > v for k in range(1, swing_len + 1)):
            pl_idx.add(i)

    # ── 2. Előre-haladó menet: bias, EQH/EQL, sweep ───────────────────────────
    bias_a       = np.zeros(n_bars, dtype=np.float32)
    eqh_dist_a   = np.zeros(n_bars, dtype=np.float32)
    eql_dist_a   = np.zeros(n_bars, dtype=np.float32)
    near_eqh_a   = np.zeros(n_bars, dtype=np.float32)
    near_eql_a   = np.zeros(n_bars, dtype=np.float32)
    sweep_bull_a = np.zeros(n_bars, dtype=np.float32)
    sweep_bear_a = np.zeros(n_bars, dtype=np.float32)
    hh_a         = np.zeros(n_bars, dtype=np.float32)
    hl_a         = np.zeros(n_bars, dtype=np.float32)

    bias = 0
    eqH = 0.0; eqL = 0.0
    ph_q: list = []   # utolsó 2 pivot high (ár)
    pl_q: list = []   # utolsó 2 pivot low
    last_hh = 0; last_hl = 0
    bull_sw_end = -1; bear_sw_end = -1

    for i in range(swing_len, n_bars):
        a = atr_v[i]
        if a <= 0 or np.isnan(a):
            bias_a[i] = bias
            continue

        # Pivot esemény a MEGERŐSÍTÉS gyertyáján: a j = i−swing_len sor pivotja
        # itt válik ismertté (kauzális — nincs look-ahead).
        j = i - swing_len

        # Pivot High esemény
        if j in ph_idx:
            ph_q.append(hi[j])
            if len(ph_q) > 2:
                ph_q.pop(0)
            if len(ph_q) == 2:
                if ph_q[1] > ph_q[0]:    # Higher High
                    last_hh = 1
                else:                     # Lower High → bearish nyomás
                    last_hh = 0
                    if bias == 1:
                        bias = -1
                if abs(ph_q[1] - ph_q[0]) / max(ph_q[0], 1e-9) <= eq_tol:
                    eqH = max(ph_q[0], ph_q[1])

        # Pivot Low esemény
        if j in pl_idx:
            pl_q.append(lo[j])
            if len(pl_q) > 2:
                pl_q.pop(0)
            if len(pl_q) == 2:
                if pl_q[1] > pl_q[0]:    # Higher Low → bullish
                    last_hl = 1
                    if bias <= 0:
                        bias = 1
                else:                     # Lower Low
                    last_hl = 0
                    if bias == 1:
                        bias = -1
                if abs(pl_q[1] - pl_q[0]) / max(pl_q[0], 1e-9) <= eq_tol:
                    eqL = min(pl_q[0], pl_q[1])

        # Sweep detektálás
        if (eqL > 0 and lo[i] < eqL and cl[i] > eqL
                and (cl[i] - lo[i]) / a >= sweep_ratio):
            eqL = 0.0
            bull_sw_end = i + sweep_memory
        if (eqH > 0 and hi[i] > eqH and cl[i] < eqH
                and (hi[i] - cl[i]) / a >= sweep_ratio):
            eqH = 0.0
            bear_sw_end = i + sweep_memory

        # Kimeneti tömbök
        bias_a[i] = bias
        hh_a[i]   = last_hh
        hl_a[i]   = last_hl
        if eqH > 0:
            d = (eqH - cl[i]) / pip_size
            eqh_dist_a[i] = d
            near_eqh_a[i] = 1 if abs(d) <= 5 else 0
        if eqL > 0:
            d = (cl[i] - eqL) / pip_size
            eql_dist_a[i] = d
            near_eql_a[i] = 1 if abs(d) <= 5 else 0
        sweep_bull_a[i] = 1 if i <= bull_sw_end else 0
        sweep_bear_a[i] = 1 if i <= bear_sw_end else 0

    df["smc_bias"]       = bias_a
    df["smc_eqh_dist"]   = eqh_dist_a
    df["smc_eql_dist"]   = eql_dist_a
    df["smc_near_eqh"]   = near_eqh_a
    df["smc_near_eql"]   = near_eql_a
    df["smc_sweep_bull"] = sweep_bull_a
    df["smc_sweep_bear"] = sweep_bear_a
    df["smc_hh"]         = hh_a
    df["smc_hl"]         = hl_a
    return df


# ---------------------------------------------------------------------------
# DLO (Directional Liquidity Oscillator) — ADX/DMI alapú irányerő
# ---------------------------------------------------------------------------

def compute_dlo(df: pd.DataFrame,
                dmi_len: int = 14, mean_lb: int = 100,
                lr_slope: float = 0.26, scale: float = 2.5,
                smooth: int = 3, thresh: float = 0.14) -> pd.DataFrame:
    """DLO — ugyanaz a logika, mint a GainzAlgo_Inspired_Indicator.mq4-ben."""
    hi, lo, cl = df["high"], df["low"], df["close"]

    tr = pd.concat([(hi - lo), (hi - cl.shift()).abs(),
                    (lo - cl.shift()).abs()], axis=1).max(axis=1)
    up_move  = hi.diff().clip(lower=0)
    dn_move  = (-lo.diff()).clip(lower=0)
    plus_dm  = up_move.where(up_move > dn_move, 0.0)
    minus_dm = dn_move.where(dn_move > up_move, 0.0)

    alpha    = 1.0 / dmi_len
    atr_w    = tr.ewm(alpha=alpha, adjust=False).mean()
    plus_di  = 100 * plus_dm.ewm(alpha=alpha, adjust=False).mean()  / atr_w.replace(0, np.nan)
    minus_di = 100 * minus_dm.ewm(alpha=alpha, adjust=False).mean() / atr_w.replace(0, np.nan)
    dx       = (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan) * 100
    adx      = dx.ewm(alpha=alpha, adjust=False).mean()

    m_plus  = plus_di.rolling(mean_lb).mean().replace(0, np.nan)
    m_minus = minus_di.rolling(mean_lb).mean().replace(0, np.nan)
    m_adx   = adx.rolling(mean_lb).mean().replace(0, np.nan)

    def sig(x):
        return 1.0 / (1.0 + np.exp(-x))

    p_plus  = sig(lr_slope * plus_di  / m_plus)
    p_minus = sig(lr_slope * minus_di / m_minus)
    p_adx   = sig(lr_slope * adx      / m_adx)

    raw_dlo = np.tanh((p_plus - p_minus) * p_adx * scale)
    dlo_val = raw_dlo.ewm(span=smooth, adjust=False).mean()

    df["dlo_val"]  = dlo_val
    df["dlo_bull"] = (dlo_val >  thresh).astype(int)
    df["dlo_bear"] = (dlo_val < -thresh).astype(int)
    return df


# ---------------------------------------------------------------------------
# H1 kontextus — az M15 gyertyákból resample-elve (nincs külön adatcsatorna)
# ---------------------------------------------------------------------------

def h1_context_from_m15(df_m15: pd.DataFrame) -> pd.DataFrame:
    """H1 trend/RSI kontextus az M15 sávokból.

    A resample H1 vödröket képez az M15 gyertyákból, majd a feature-öket EGY
    vödörrel eltoljuk (shift): egy M15 gyertyához mindig a LEGUTOLSÓ TELJESEN
    LEZÁRT H1 vödör értéke tartozik → nincs look-ahead. (Max ~1ó45p „késés" —
    a H1 trend lassú kontextus, ezt elbírja; cserébe a tanítás, a backtest és a
    live garantáltan ugyanazt látja.)"""
    h1 = pd.DataFrame({
        "close": df_m15["close"].resample("1h").last(),
    }).dropna(subset=["close"])

    h1["h1_ema8"]  = ema(h1["close"], 8)
    h1["h1_ema21"] = ema(h1["close"], 21)
    h1["h1_ema50"] = ema(h1["close"], 50)
    h1["h1_rsi"]   = rsi(h1["close"])
    h1["h1_trend"]        = (h1["h1_ema8"] > h1["h1_ema21"]).astype(int)
    h1["h1_rsi_v50"]      = h1["h1_rsi"] - 50
    h1["h1_trend_strong"] = ((h1["h1_ema8"] > h1["h1_ema21"]) &
                             (h1["h1_ema21"] > h1["h1_ema50"])).astype(int)

    # Csak LEZÁRT vödörből (shift) → look-ahead-mentes
    cols = ["h1_trend", "h1_rsi_v50", "h1_trend_strong"]
    return h1[cols].shift(1)


# ---------------------------------------------------------------------------
# Feature-mátrix — a modell bemeneti oszlopai (sorrend = tanítási sorrend!)
# ---------------------------------------------------------------------------

FEATURES = [
    # M15 trend
    "ema8_21_diff", "ema21_50_diff", "price_ema8", "price_ema21",
    "trend_up", "trend_strong",
    # H1 multi-timeframe
    "h1_trend", "h1_rsi_v50", "h1_trend_strong", "mtf_align_long", "mtf_align_short",
    # Momentum
    "rsi14", "rsi7", "rsi_diff", "rsi_vs_50",
    "rsi_cross_up", "rsi_cross_dn",
    # Williams %R(55) + WMA(100)
    "wpr55", "wpr_wma100", "wpr_vs_wma", "wpr_cross_up", "wpr_cross_dn", "wpr_zone",
    # SMC / LiquiditySMC
    "smc_bias", "smc_eqh_dist", "smc_eql_dist",
    "smc_near_eqh", "smc_near_eql",
    "smc_sweep_bull", "smc_sweep_bear",
    "smc_hh", "smc_hl",
    # Gyertyaszerkezet
    "body", "upper_wick", "lower_wick", "bar_range",
    # Volatilitás
    "atr_ratio", "atr_vs_avg", "vol_regime",
    # Árváltozás
    "close_ret1", "close_ret3", "close_ret8",
    "close_lag1", "close_lag2", "close_lag3", "close_lag4", "close_lag8",
    # DLO
    "dlo_val", "dlo_bull", "dlo_bear",
]

# Az indikátor-bemelegítéshez szükséges M15 sorok száma: a leghosszabb ablakos
# indikátor (WPR55+WMA100=155, DLO mean_lb=100+DMI, H1 EMA50≈50 vödör = 200 M15)
# + az EMA-k gyakorlati konvergenciája (span×3). Az ez ALATTI sorok feature-ei
# torzak/NaN-ok → a hívó szeletelje le.
WARMUP_BARS = 700


def build_feature_frame(df_m15: pd.DataFrame, pip_size: float) -> pd.DataFrame:
    """A teljes feature-készlet kiszámítása egy M15 OHLC frame-re.

    A bemenet UTC/szerver-idő indexű M15 OHLC(V) — CSAK ZÁRT gyertyák (a hívó
    felelőssége a formálódó gyertya levágása). A visszaadott frame a bemenet
    másolata + az összes feature oszlop; az i. sor feature-ei az i. gyertya
    ZÁRÁSÁIG ismert adatból számolódnak (nincs look-ahead)."""
    df = df_m15.copy()
    pip = float(pip_size)

    # ── Alap indikátorok ──────────────────────────────────────────────────
    df["ema8"]    = ema(df["close"], 8)
    df["ema21"]   = ema(df["close"], 21)
    df["ema50"]   = ema(df["close"], 50)
    df["ema200"]  = ema(df["close"], 200)
    df["rsi14"]   = rsi(df["close"], 14)
    df["rsi7"]    = rsi(df["close"], 7)
    df["atr14"]   = atr(df, 14)
    df["atr5"]    = atr(df, 5)
    df["atr_sma"] = df["atr14"].rolling(20).mean()

    # ── Ár-eredetű feature-ök ─────────────────────────────────────────────
    df["body"]       = (df["close"] - df["open"]) / pip
    df["upper_wick"] = (df["high"] - df[["open", "close"]].max(axis=1)) / pip
    df["lower_wick"] = (df[["open", "close"]].min(axis=1) - df["low"]) / pip
    df["bar_range"]  = (df["high"] - df["low"]) / pip

    # ── Trend ─────────────────────────────────────────────────────────────
    df["ema8_21_diff"]  = (df["ema8"] - df["ema21"]) / pip
    df["ema21_50_diff"] = (df["ema21"] - df["ema50"]) / pip
    df["price_ema8"]    = (df["close"] - df["ema8"]) / pip
    df["price_ema21"]   = (df["close"] - df["ema21"]) / pip
    df["trend_up"]      = (df["ema8"] > df["ema21"]).astype(int)
    df["trend_strong"]  = ((df["ema8"] > df["ema21"]) &
                           (df["ema21"] > df["ema50"])).astype(int)

    # ── Momentum ──────────────────────────────────────────────────────────
    df["rsi_diff"]   = df["rsi14"].diff()
    df["rsi_vs_50"]  = df["rsi14"] - 50
    df["close_ret1"] = df["close"].pct_change(1) * 10000
    df["close_ret3"] = df["close"].pct_change(3) * 10000
    df["close_ret8"] = df["close"].pct_change(8) * 10000

    # ── Volatilitás ───────────────────────────────────────────────────────
    df["atr_ratio"]  = df["atr5"] / df["atr14"]
    df["atr_vs_avg"] = df["atr14"] / df["atr_sma"]
    df["vol_regime"] = (df["atr14"] > df["atr_sma"]).astype(int)

    # ── Session / idő (szerver-idő órák — a tanítás és a live ugyanazt látja)
    df["hour"]        = df.index.hour
    df["dow"]         = df.index.dayofweek
    df["london_core"] = ((df["hour"] >= 10) & (df["hour"] < 13)).astype(int)
    df["ny_overlap"]  = ((df["hour"] >= 15) & (df["hour"] < 18)).astype(int)

    # ── Vissza-tekintő záróár-minták ──────────────────────────────────────
    for lag in [1, 2, 3, 4, 8]:
        df[f"close_lag{lag}"] = (df["close"] - df["close"].shift(lag)) / pip

    # ── RSI keresztek ─────────────────────────────────────────────────────
    df["rsi_cross_up"] = ((df["rsi14"] > 50) & (df["rsi14"].shift() <= 50)).astype(int)
    df["rsi_cross_dn"] = ((df["rsi14"] < 50) & (df["rsi14"].shift() >= 50)).astype(int)

    # ── Williams %R(55) + WMA(100) ────────────────────────────────────────
    df["wpr55"]        = williams_r(df, 55)
    df["wpr_wma100"]   = wma(df["wpr55"], 100)
    df["wpr_vs_wma"]   = df["wpr55"] - df["wpr_wma100"]
    df["wpr_cross_up"] = ((df["wpr55"] > df["wpr_wma100"]) &
                          (df["wpr55"].shift() <= df["wpr_wma100"].shift())).astype(int)
    df["wpr_cross_dn"] = ((df["wpr55"] < df["wpr_wma100"]) &
                          (df["wpr55"].shift() >= df["wpr_wma100"].shift())).astype(int)
    # include_lowest: a pontosan -100-as WPR is kapjon zónát (a forrásban NaN
    # lett → a sor kiesett a tanításból/predikcióból; itt egységesen érvényes).
    df["wpr_zone"]     = pd.cut(df["wpr55"], bins=[-100, -80, -20, 0],
                                labels=[-1, 0, 1], include_lowest=True).astype(float)

    # ── SMC + DLO ─────────────────────────────────────────────────────────
    df = compute_smc(df, pip)
    df = compute_dlo(df)

    # ── H1 kontextus (M15-ből resample-elve, lezárt vödörből) ─────────────
    h1_ctx = h1_context_from_m15(df_m15)
    df["h1_trend"]        = h1_ctx["h1_trend"].reindex(df.index, method="ffill").fillna(0)
    df["h1_rsi_v50"]      = h1_ctx["h1_rsi_v50"].reindex(df.index, method="ffill").fillna(0)
    df["h1_trend_strong"] = h1_ctx["h1_trend_strong"].reindex(df.index, method="ffill").fillna(0)
    df["mtf_align_long"]  = ((df["trend_up"] == 1) & (df["h1_trend"] == 1)).astype(int)
    df["mtf_align_short"] = ((df["trend_up"] == 0) & (df["h1_trend"] == 0)).astype(int)

    return df
