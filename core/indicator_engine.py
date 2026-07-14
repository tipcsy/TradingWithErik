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


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index (Wilder-simítás), értékkészlet 0..100."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - 100.0 / (1.0 + rs)
    out[avg_loss == 0.0] = 100.0          # nincs veszteség → RSI=100
    return out


def cci(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 20) -> pd.Series:
    """Commodity Channel Index — a tipikus ár eltérése a mozgóátlagától, közép=0."""
    tp = (high + low + close) / 3.0
    ma = tp.rolling(period).mean()
    md = (tp - ma).abs().rolling(period).mean()
    return (tp - ma) / (0.015 * md.replace(0.0, np.nan))


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def supertrend(high: pd.Series, low: pd.Series, close: pd.Series,
               period: int = 10, multiplier: float = 3.0):
    """Supertrend indikátor — ATR-alapú trend-követő sáv.

    Visszaad: (line, direction) két pd.Series:
      • line      : az aktuális Supertrend-vonal ára (a felső VAGY alsó sáv),
      • direction : +1 = emelkedő trend (a vonal az ár ALATT), -1 = csökkenő
                    (a vonal az ár FÖLÖTT).
    A `direction` VÁLTÁSA (flip) a be-/kiszállási jel: long pozíciónál a +1→-1
    váltás a kiszállás. A warmup (ATR NaN) tartományban direction=+1, line=NaN.
    Standard algoritmus (végleges sávok átvitellel), a `core.indicator_engine.atr`
    (SMA-simítású TR) baseline-jával — konzisztens a projekt többi ATR-jével."""
    atr_ = atr(high, low, close, period).to_numpy()
    hl2 = ((high + low) / 2.0).to_numpy()
    c = close.to_numpy()
    n = len(c)
    basic_upper = hl2 + multiplier * atr_
    basic_lower = hl2 - multiplier * atr_

    final_upper = np.full(n, np.nan)
    final_lower = np.full(n, np.nan)
    line = np.full(n, np.nan)
    direction = np.ones(n, dtype=int)   # alapból +1 (emelkedő)

    prev_valid = False
    for i in range(n):
        if np.isnan(atr_[i]):
            continue                     # warmup — még nincs ATR
        if not prev_valid:
            # Első érvényes sor: induljunk az alsó sávról, emelkedő iránnyal.
            final_upper[i] = basic_upper[i]
            final_lower[i] = basic_lower[i]
            direction[i] = 1
            line[i] = final_lower[i]
            prev_valid = True
            continue
        # Végleges felső sáv: szűkül, hacsak az előző zárás ki nem törte fölfelé.
        final_upper[i] = (basic_upper[i]
                          if (basic_upper[i] < final_upper[i-1] or c[i-1] > final_upper[i-1])
                          else final_upper[i-1])
        # Végleges alsó sáv: emelkedik, hacsak az előző zárás ki nem törte lefelé.
        final_lower[i] = (basic_lower[i]
                          if (basic_lower[i] > final_lower[i-1] or c[i-1] < final_lower[i-1])
                          else final_lower[i-1])
        # Irány: az előző vonal áttörése vált.
        if direction[i-1] == 1:
            direction[i] = -1 if c[i] < final_lower[i] else 1
        else:
            direction[i] = 1 if c[i] > final_upper[i] else -1
        line[i] = final_lower[i] if direction[i] == 1 else final_upper[i]

    idx = close.index
    return pd.Series(line, index=idx), pd.Series(direction, index=idx)


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
