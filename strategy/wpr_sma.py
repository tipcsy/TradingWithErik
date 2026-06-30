"""
WPR + SMA stratégia — a jelenlegi (Erik-féle) logika a seam mögé csomagolva.

A jelzés- és indikátor-matematika VÁLTOZATLAN: a core.indicator_engine és a
core.signal_detector függvényeit hívja. Ez a modul csak "becsomagolja" őket a
Strategy interfészbe, hogy a váz (dashboard/run/optimizer/backtest) generikusan
tudja használni.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional, Any

import pandas as pd

from strategy.base import (
    Strategy, Column, StrategyColumn, CountdownColumn, MarketData, Cell, Timeframe,
)
from core.indicator_engine import compute_indicators
from core.signal_detector import PairState, check_m15_signal, check_m1_entry


# ---------------------------------------------------------------------------
# Élő jelzésállapot (a futtatómotor tartja életben páronként)
# ---------------------------------------------------------------------------

@dataclass
class WprSmaState:
    symbol:        str
    signal:        PairState = field(init=False)
    prev_m1_wpr:   Optional[float] = None
    last_m15_time: Optional[pd.Timestamp] = None

    def __post_init__(self):
        self.signal = PairState(self.symbol)


def _clamp_wpr(v: float) -> float:
    """WPR a [-100, 0] tartományba szorítva, a -0.0 normalizálva 0.0-ra."""
    if v is None or math.isnan(v):
        return float("nan")
    v = max(-100.0, min(0.0, float(v)))
    return 0.0 if v == 0 else v


def _wpr_cell(value: float) -> Cell:
    if value is None or math.isnan(value):
        return Cell("—", "muted")
    return Cell(f"{value:.1f}", "white")


def _signal_cell(direction: str, active: bool) -> Cell:
    if not active or direction not in ("BUY", "SELL"):
        return Cell("—", "muted")
    arrow = "▲" if direction == "BUY" else "▼"
    return Cell(f"{direction}{arrow}", "green" if direction == "BUY" else "red")


class WprSmaStrategy(Strategy):
    name = "wpr_sma"

    # --- Megjelenítés -----------------------------------------------------

    def timeframes(self) -> list[Timeframe]:
        return [Timeframe("M15", 15), Timeframe("M1", 1)]

    def columns(self) -> list[Column]:
        # A visszaszámlálók (gyertyazárásig hátralévő idő) a VÁZ közös felső
        # sávjába kerülnek (minden instrumentumnál azonosak) — nem oszlopként.
        return [
            StrategyColumn("sma_dir",  "SMA irány",  8),
            StrategyColumn("wpr_m15",  "M15 WPR",    7),
            StrategyColumn("sig_m15",  "M15 jelzés", 9),
            StrategyColumn("wpr_m1",   "M1 WPR",     7),
            StrategyColumn("sig_m1",   "M1 jelzés",  8),
        ]

    def warmup_bars(self, params: dict, timeframe_label: str) -> int:
        if timeframe_label == "M15":
            return max(params.get("sma_period", 200),
                       params.get("wpr_m15_period", 21),
                       params.get("atr_period", 14)) + 5
        if timeframe_label == "M1":
            return params.get("wpr_m1_period", 8) + 5
        return 50

    def compute_display(self, md: MarketData) -> dict[str, Cell]:
        """Megjelenítési cellák.

        A WPR-t a FORMÁLÓDÓ gyertyán mutatjuk (élő, gyakori frissítésnél mozog).
        A JELZÉSEKET viszont a ZÁRT gyertyák során VÉGIGJÁTSZVA számoljuk —
        így az M15 jelzési ablak állapota PONTOS (egyetlen gyertyából nem lehet
        rekonstruálni). Ez ugyanazt az állapotot adja, mint az éles motor."""
        empty = {
            "sma_dir":  Cell("—", "muted"),
            "wpr_m15":  Cell("—", "muted"),
            "sig_m15":  Cell("—", "muted"),
            "wpr_m1":   Cell("—", "muted"),
            "sig_m1":   Cell("—", "muted"),
        }
        df_m15 = md.bars.get("M15")
        df_m1  = md.bars.get("M1")
        if df_m15 is None or df_m1 is None or len(df_m15) < 3 or len(df_m1) < 3:
            return empty

        try:
            m15, m1 = compute_indicators(df_m15, df_m1, md.params)
        except Exception:
            return empty

        # ── M15 jelzési állapot rekonstrukciója a ZÁRT gyertyák végigjátszásával
        closes = m15["close"].values
        smas   = m15["sma"].values
        wprs15 = m15["wpr"].values
        state  = PairState(md.symbol)
        seen_closed = False
        for i in range(len(m15) - 1):            # az utolsó sor a formálódó gyertya
            s, w = smas[i], wprs15[i]
            if math.isnan(s) or math.isnan(w):
                continue
            state = check_m15_signal(state, close=float(closes[i]), sma=float(s),
                                     wpr_m15=float(w), params=md.params)
            seen_closed = True
        if not seen_closed:
            return empty
        direction = state.direction

        # ── M1 belépési jel az utolsó két ZÁRT M1 gyertyából, a valós M15 ablakkal
        m1_wprs = m1["wpr"].values
        m1_signal = "NONE"
        if len(m1_wprs) >= 3:
            prev_w, cur_w = m1_wprs[-3], m1_wprs[-2]   # -1 a formálódó
            if not math.isnan(prev_w) and not math.isnan(cur_w):
                m1_signal = check_m1_entry(state, float(prev_w), float(cur_w), md.params)

        # ── WPR a formálódó gyertyán; ha NaN, vissza a zártra (spike-szűrés) ──
        wpr_m15_disp = _clamp_wpr(wprs15[-1])
        if math.isnan(wpr_m15_disp):
            wpr_m15_disp = _clamp_wpr(wprs15[-2])
        wpr_m1_disp = _clamp_wpr(m1_wprs[-1])
        if math.isnan(wpr_m1_disp):
            wpr_m1_disp = _clamp_wpr(m1_wprs[-2])

        sma_cell = Cell(direction, "green" if direction == "BUY"
                        else "red" if direction == "SELL" else "muted")
        if direction == "NONE":
            sma_cell = Cell("—", "muted")

        return {
            "sma_dir": sma_cell,
            "wpr_m15": _wpr_cell(wpr_m15_disp),
            "sig_m15": _signal_cell(direction, state.m15_window_open),
            "wpr_m1":  _wpr_cell(wpr_m1_disp),
            "sig_m1":  _signal_cell(m1_signal, m1_signal in ("BUY", "SELL")),
        }

    # --- Élő jelzéslogika (ZÁRT gyertyán, állapottartó) -------------------

    def new_signal_state(self, symbol: str) -> WprSmaState:
        return WprSmaState(symbol)

    def on_bar_close(self, state: WprSmaState, md: MarketData) -> tuple[WprSmaState, str]:
        """A process_pair() jelzéslogikájának pontos mása, állapottartással.
        Visszaad: (state, "BUY"|"SELL"|"NONE")."""
        df_m15 = md.bars.get("M15")
        df_m1  = md.bars.get("M1")
        if df_m15 is None or df_m1 is None or len(df_m15) < 2 or len(df_m1) < 3:
            return state, "NONE"

        m15, m1 = compute_indicators(df_m15, df_m1, md.params)
        m15_closed = m15.iloc[-2]
        m15_time   = m15.index[-2]
        m1_closed  = m1.iloc[-2]
        m1_prev    = m1.iloc[-3]

        if any(pd.isna(m15_closed.get(k)) for k in ("sma", "wpr", "atr")):
            return state, "NONE"
        if pd.isna(m1_closed.get("wpr")) or pd.isna(m1_prev.get("wpr")):
            return state, "NONE"

        # M15 állapot csak ÚJ M15 gyertyazáráskor frissül
        if state.last_m15_time != m15_time:
            state.last_m15_time = m15_time
            state.signal = check_m15_signal(
                state.signal,
                close=float(m15_closed["close"]),
                sma=float(m15_closed["sma"]),
                wpr_m15=float(m15_closed["wpr"]),
                params=md.params,
            )

        signal = "NONE"
        if state.prev_m1_wpr is not None:
            signal = check_m1_entry(state.signal, state.prev_m1_wpr,
                                    float(m1_closed["wpr"]), md.params)
        state.prev_m1_wpr = float(m1_closed["wpr"])
        return state, signal

    # --- Optimalizálás ----------------------------------------------------

    def base_params(self, cfg: dict) -> dict:
        return {**cfg.get("indicators", {}), **cfg.get("sltp", {}),
                **cfg.get("position_mgmt", {})}

    def param_space(self, cfg: dict, base_params: dict, method: str,
                    max_trials: int) -> list[dict]:
        from ml.optimizer import generate_random_params, generate_grid_params
        opt_cfg = cfg["optimizer"]
        if method == "grid":
            return generate_grid_params(opt_cfg, base_params)
        return generate_random_params(opt_cfg, base_params, max_trials)
