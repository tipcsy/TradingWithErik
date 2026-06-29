import numpy as np
import pandas as pd


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period).mean()


def wpr(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
    """Williams Percent Range: értékkészlet -100..0"""
    highest_high = high.rolling(period).max()
    lowest_low = low.rolling(period).min()
    rng = highest_high - lowest_low
    result = np.where(rng == 0, -50.0, (highest_high - close) / rng * -100.0)
    return pd.Series(result, index=close.index)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def compute_indicators(
    df_m15: pd.DataFrame,
    df_m1: pd.DataFrame,
    params: dict,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    SMA és WPR kiszámítása mindkét időkeretre.
    Visszaad: (df_m15_ind, df_m1_ind) — eredeti sorok + új oszlopok
    """
    m15 = df_m15.copy()
    m1 = df_m1.copy()

    m15["sma"] = sma(m15["close"], params["sma_period"])
    m15["wpr"] = wpr(m15["high"], m15["low"], m15["close"], params["wpr_m15_period"])
    m15["atr"] = atr(m15["high"], m15["low"], m15["close"], params["atr_period"])

    m1["wpr"] = wpr(m1["high"], m1["low"], m1["close"], params["wpr_m1_period"])

    return m15, m1
