"""
WPR + SMA stratégia — a jelenlegi (Erik-féle) logika a seam mögé csomagolva.

A jelzés- és indikátor-matematika VÁLTOZATLAN: a core.indicator_engine és a
core.signal_detector függvényeit hívja. Ez a modul csak "becsomagolja" őket a
Strategy interfészbe, hogy a váz (dashboard/run/optimizer/backtest) generikusan
tudja használni.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field, replace
from typing import Optional, Any

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

from strategy.base import (
    Strategy, Column, StrategyColumn, CountdownColumn, MarketData, Cell, Timeframe,
)
from strategy import visual as viz
from core.indicator_engine import compute_indicators
from core.signal_detector import PairState, check_m15_signal, check_m1_entry
from core.risk_manager import calc_sl_tp_pips


# ---------------------------------------------------------------------------
# Élő jelzésállapot (a futtatómotor tartja életben páronként)
# ---------------------------------------------------------------------------

@dataclass
class WprSmaState:
    symbol:        str
    signal:        PairState = field(init=False)
    prev_m1_wpr:   Optional[float] = None
    last_m15_time: Optional[pd.Timestamp] = None
    last_signal:   str = "NONE"   # utolsó M1 belépési jel (a kijelzés latch-eli)
    last_signal_m1_time: Optional[pd.Timestamp] = None  # melyik M1 gyertyán szólt

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

        # ── ÉLŐ (formálódó gyertyás) lépés — CSAK a KIJELZÉSHEZ ──────────────
        # A kereskedés ZÁRT gyertyán dől el (state), de a tábla a WPR-t a
        # FORMÁLÓDÓ gyertyán mutatja. Hogy a jelzés-nyíl ezzel KONZISZTENS
        # legyen — és a teljes nyitott ablak alatt látsszon, ne csak a
        # gyertyazárás pillanatában — a jelzési ablakot egy provizórikus
        # lépéssel az élő gyertyára is kiértékeljük. A `state` (zárt) érintetlen.
        live_state = replace(state)
        s_live, w_live, c_live = smas[-1], wprs15[-1], closes[-1]
        if not (math.isnan(s_live) or math.isnan(w_live)):
            live_state = check_m15_signal(
                live_state, close=float(c_live), sma=float(s_live),
                wpr_m15=float(w_live), params=md.params)
        direction = live_state.direction

        # ── M1 belépési jel az utolsó két ZÁRT M1 gyertyából, az ÉLŐ M15 ablakkal
        m1_wprs = m1["wpr"].values
        m1_signal = "NONE"
        if len(m1_wprs) >= 3:
            prev_w, cur_w = m1_wprs[-3], m1_wprs[-2]   # -1 a formálódó
            if not math.isnan(prev_w) and not math.isnan(cur_w):
                m1_signal = check_m1_entry(live_state, float(prev_w), float(cur_w), md.params)

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
            "sig_m15": _signal_cell(direction, live_state.m15_window_open),
            "wpr_m1":  _wpr_cell(wpr_m1_disp),
            "sig_m1":  _signal_cell(m1_signal, m1_signal in ("BUY", "SELL")),
        }

    # --- Élő jelzéslogika (ZÁRT gyertyán, állapottartó) -------------------

    def new_signal_state(self, symbol: str) -> WprSmaState:
        return WprSmaState(symbol)

    def on_bar_close(self, state: WprSmaState, md: MarketData) -> tuple[WprSmaState, str]:
        """ZÁRT gyertyán, állapottartó jelzéslogika. Visszaad: (state, jel).

        ELSŐ híváskor (indítás/restart után) BEMELEGÍT: visszajátssza a zárt
        M15 gyertyákat, hogy a jelzési ablak állapota azonnal megegyezzen a
        kijelzéssel — különben a motor "nem látja" a már folyamatban lévő
        szetupot, és nem lép be (miközben a tábla BUY▲/SELL▼-t mutat)."""
        df_m15 = md.bars.get("M15")
        df_m1  = md.bars.get("M1")
        if df_m15 is None or df_m1 is None or len(df_m15) < 2 or len(df_m1) < 3:
            return state, "NONE"

        m15, m1 = compute_indicators(df_m15, df_m1, md.params)
        m15_closed = m15.iloc[-2]
        m15_time   = m15.index[-2]
        m1_closed  = m1.iloc[-2]
        m1_prev    = m1.iloc[-3]
        m1_time    = m1.index[-2]   # az aktuális ZÁRT M1 gyertya ideje

        if any(pd.isna(m15_closed.get(k)) for k in ("sma", "wpr", "atr")):
            return state, "NONE"
        if pd.isna(m1_closed.get("wpr")) or pd.isna(m1_prev.get("wpr")):
            return state, "NONE"

        if state.last_m15_time is None:
            # ── BEMELEGÍTÉS: a teljes zárt M15 előzmény visszajátszása ──
            closes = m15["close"].values
            smas   = m15["sma"].values
            wprs   = m15["wpr"].values
            for i in range(len(m15) - 1):          # az utolsó sor a formálódó
                if math.isnan(smas[i]) or math.isnan(wprs[i]):
                    continue
                state.signal = check_m15_signal(
                    state.signal, close=float(closes[i]), sma=float(smas[i]),
                    wpr_m15=float(wprs[i]), params=md.params)
            state.last_m15_time = m15_time
            # az utolsó M1 átmenet (prev=−3, cur=−2) is kiértékelhető legyen
            state.prev_m1_wpr = float(m1_prev["wpr"])
        elif state.last_m15_time != m15_time:
            # Inkrementális: csak ÚJ M15 gyertyazáráskor
            state.last_m15_time = m15_time
            state.signal = check_m15_signal(
                state.signal,
                close=float(m15_closed["close"]),
                sma=float(m15_closed["sma"]),
                wpr_m15=float(m15_closed["wpr"]),
                params=md.params,
            )

        cur_m1_wpr = float(m1_closed["wpr"])
        signal = "NONE"
        if state.prev_m1_wpr is not None:
            signal = check_m1_entry(state.signal, state.prev_m1_wpr,
                                    cur_m1_wpr, md.params)
        if signal != "NONE":
            log.info(
                "📊 %s → %s jelzés | M15 zárt WPR: %.1f (ablak: %s) | M1 WPR: %.1f → %.1f",
                md.symbol, signal,
                float(m15_closed["wpr"]),
                "NYITVA" if state.signal.m15_window_open else "ZÁRVA",
                state.prev_m1_wpr if state.prev_m1_wpr is not None else float("nan"),
                cur_m1_wpr,
            )
        state.prev_m1_wpr = cur_m1_wpr

        # M1 jel latch a kijelzéshez: az M1 belépési jel EGY M1 gyertyás esemény,
        # ezért csak addig látszik, amíg ugyanaz a ZÁRT M1 gyertya az aktuális
        # (~1 perc). A KÖVETKEZŐ M1 gyertya zárásakor törlődik — akkor is, ha az
        # M15 ablak még nyitva van (a régi „ablakig tart" latch volt a hiba).
        if signal != "NONE":
            state.last_signal = signal
            state.last_signal_m1_time = m1_time
        elif state.last_signal_m1_time is not None and m1_time != state.last_signal_m1_time:
            state.last_signal = "NONE"
            state.last_signal_m1_time = None

        return state, signal

    # --- Megjelenítés a MOTOR élő állapotából ------------------------------

    def live_cells(self, state: WprSmaState, md: MarketData) -> dict[str, Cell]:
        """Cellák a MOTOR jelzésállapotából (state.signal) — a tábla PONTOSAN azt
        mutatja, amivel a motor kereskedik. A WPR a formálódó gyertyán mozog; az
        SMA-irány, az M15 jelzési ablak és az M1 jel a motor ÉLŐ állapotából jön
        (nincs külön rekonstrukció → nincs eltérés)."""
        empty = {
            "sma_dir":  Cell("—", "muted"),
            "wpr_m15":  Cell("—", "muted"),
            "sig_m15":  Cell("—", "muted"),
            "wpr_m1":   Cell("—", "muted"),
            "sig_m1":   Cell("—", "muted"),
        }
        df_m15 = md.bars.get("M15")
        df_m1  = md.bars.get("M1")
        if df_m15 is None or df_m1 is None or len(df_m15) < 2 or len(df_m1) < 2:
            return empty
        try:
            m15, m1 = compute_indicators(df_m15, df_m1, md.params)
        except Exception:
            return empty

        sig = state.signal            # a motor élő jelzésállapota (PairState)
        direction = sig.direction

        # WPR a formálódó gyertyán; NaN esetén vissza a zártra (spike-szűrés)
        wprs15 = m15["wpr"].values
        m1_wprs = m1["wpr"].values
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
            "sig_m15": _signal_cell(direction, sig.m15_window_open),
            "wpr_m1":  _wpr_cell(wpr_m1_disp),
            "sig_m1":  _signal_cell(state.last_signal,
                                    state.last_signal in ("BUY", "SELL")),
        }

    # --- MT5 chart-vizualizáció -------------------------------------------

    def visual_lookback_bars(self, params: dict, timeframe_label: str) -> int:
        """Mélyebb ablak, mint a jelzés-warmup — hogy a szalag több napra
        visszamenjen (az SMA miatt a warmupból csak pár érvényes sor lenne)."""
        if timeframe_label == "M15":
            return params.get("sma_period", 200) + 300
        if timeframe_label == "M1":
            # ~1 nap M1 az utóbbi belépő-jelzésekhez (feltétel 3); a TP/SL a
            # 6-gyertyás szélessége miatt M1 charton nézve látszik igazán.
            return params.get("wpr_m1_period", 8) + 1440
        return 0

    def visual_objects(self, md: MarketData) -> list:
        """A wpr_sma teljes chart-vizualizációja:

          • Feltétel 1 — SMA-irány SZALAG a chart alján: az M15 zárt gyertyák
            iránya (close vs SMA) azonos irányú szegmensekbe csoportosítva; zöld
            (BUY: ár az SMA felett), piros (SELL: ár alatt). Stabil név (szegmens
            kezdő-ideje) → a futó szegmens NŐ (upsert), nincs duplikátum.
          • Feltétel 2 — az M15 jelzési ablak HOSSZÁT KÉK téglalap mutatja,
            közvetlenül az SMA-szalag FELETT (nyitástól zárásig → látszik, meddig
            tart a jelzés).
          • Feltétel 3 — minden M1 BELÉPŐnél HÁROM vízszintes trendvonal a
            belépőre CENTRÁLVA (−3…+3 gyertya, 6 hosszú): NARANCS a belépő
            árszintjén, zöld TP, piros SL (ATR-ből, a motor `calc_sl_tp_pips`-ével)
            + FÜGGŐLEGES irány-jelzés a belépő idejénél (zöld BUY / piros SELL).
          • Beállítás-táblázat a chart bal-felső sarkában (Label).

        A jelzés-visszajátszás UGYANAZT a `signal_detector` logikát használja,
        amivel a motor kereskedik → a rajz konzisztens (a végrehajtási kapukat —
        session/spread/slot — a viz nem szűri: a stratégia NYERS jeleit mutatja).
        """
        df_m15 = md.bars.get("M15")
        df_m1  = md.bars.get("M1")
        if df_m15 is None or df_m1 is None or len(df_m15) < 3 or len(df_m1) < 3:
            return []
        try:
            m15, m1 = compute_indicators(df_m15, df_m1, md.params)
        except Exception:
            return []

        closes = m15["close"].values
        smas   = m15["sma"].values
        highs  = m15["high"].values
        lows   = m15["low"].values
        wprs15 = m15["wpr"].values
        atr15  = m15["atr"].values
        # NYERS bar-idő integer (a copy_rates ugyanezt adja) → pontosan a
        # gyertyára esik, időzóna-konverzió nélkül.
        times  = [int(t.timestamp()) for t in m15.index]

        valid = ~np.isnan(smas)
        if not valid.any():
            return []

        objects: list = []

        # ── Feltétel 1+2 sávok: két egymásra rakott szalag a candle-ök alatt ──
        # Fentről lefelé: KÉK (M15 jelzés) közvetlenül az SMA-szalag felett.
        lo = float(np.nanmin(lows[valid]))
        hi = float(np.nanmax(highs[valid]))
        rng = (hi - lo) or (lo * 0.001)
        gap = rng * 0.02                       # rés a candle-ök alatt
        th  = rng * 0.05                       # egy sáv vastagsága
        blue_top = lo - gap                    # kék (M15 jelzés) sáv
        blue_bot = lo - gap - th
        sma_top  = blue_bot                    # SMA-szalag közvetlenül alatta
        sma_bot  = blue_bot - th

        def _dir(i: int) -> str:
            s, c = smas[i], closes[i]
            if math.isnan(s):
                return "NONE"
            return "BUY" if c > s else "SELL" if c < s else "NONE"

        seg_start_i, seg_dir = None, "NONE"

        def _emit_band(start_i: int, end_time: int, direction: str):
            objects.append(viz.Rect(
                name=f"smaband_{times[start_i]}",
                t1=times[start_i], p1=sma_top,
                t2=end_time,       p2=sma_bot,
                color="green" if direction == "BUY" else "red", fill=True))

        for i in range(len(m15)):          # az utolsó sor a FORMÁLÓDÓ gyertya
            d = _dir(i)
            if d != seg_dir:
                if seg_dir in ("BUY", "SELL") and seg_start_i is not None:
                    _emit_band(seg_start_i, times[i], seg_dir)
                seg_dir = d
                seg_start_i = i if d in ("BUY", "SELL") else None
        if seg_dir in ("BUY", "SELL") and seg_start_i is not None:
            _emit_band(seg_start_i, times[-1], seg_dir)   # futó szegmens → NŐ

        # ── M15 jelzés-visszajátszás: feltétel 2 KÉK sávok + állapot-idővonal ──
        # Az idővonal (irány + ablak + ATR M15 zárásonként) kell a feltétel 3
        # M1 belépőihez: melyik M15 állapot volt aktív az adott M1 gyertyánál.
        # Az M15 jelzési ablak minden NYITOTT szakaszára egy kék téglalap (az
        # SMA-szalag felett) — így látszik, meddig tart az adott jelzés.
        state = PairState(md.symbol)
        prev_open = False
        win_open_i = None
        tl_t, tl_dir, tl_win, tl_atr = [], [], [], []

        def _emit_win(open_i: int, end_time: int):
            objects.append(viz.Rect(
                name=f"m15win_{times[open_i]}",
                t1=times[open_i], p1=blue_top,
                t2=end_time,      p2=blue_bot,
                color="blue", fill=True))

        for i in range(len(m15) - 1):      # csak ZÁRT M15 gyertyák
            s, w, c = smas[i], wprs15[i], closes[i]
            if math.isnan(s) or math.isnan(w):
                continue
            state = check_m15_signal(state, close=float(c), sma=float(s),
                                     wpr_m15=float(w), params=md.params)
            tl_t.append(times[i]); tl_dir.append(state.direction)
            tl_win.append(state.m15_window_open); tl_atr.append(atr15[i])
            if state.m15_window_open and not prev_open:
                win_open_i = i                                  # ablak nyílt
            elif prev_open and not state.m15_window_open and win_open_i is not None:
                _emit_win(win_open_i, times[i])                 # ablak zárult
                win_open_i = None
            prev_open = state.m15_window_open
        if prev_open and win_open_i is not None:
            _emit_win(win_open_i, times[-1])                    # még nyitva → NŐ

        # ── Feltétel 3: M1 belépők + 6-gyertyás TP/SL ──────────────────────
        pip = md.params.get("pip_size", 0.0001)
        m1_wprs  = m1["wpr"].values
        m1_close = m1["close"].values
        times1   = [int(t.timestamp()) for t in m1.index]
        if tl_t:
            p = 0
            for j in range(1, len(m1) - 1):          # az utolsó M1 formálódik
                t = times1[j]
                while p + 1 < len(tl_t) and tl_t[p + 1] <= t:
                    p += 1
                if tl_t[p] > t or not tl_win[p]:      # M1 az első állapot előtt / zárt ablak
                    continue
                pw, cw = m1_wprs[j - 1], m1_wprs[j]
                if math.isnan(pw) or math.isnan(cw):
                    continue
                st = PairState(md.symbol, direction=tl_dir[p], m15_window_open=True)
                sig = check_m1_entry(st, float(pw), float(cw), md.params)
                if sig not in ("BUY", "SELL"):
                    continue
                atr_v = tl_atr[p]
                if math.isnan(atr_v):
                    continue
                entry = float(m1_close[j])
                sl_pips, tp_pips = calc_sl_tp_pips(float(atr_v), {**md.params, "pip_size": pip})
                if sig == "BUY":
                    sl, tp = entry - sl_pips * pip, entry + tp_pips * pip
                else:
                    sl, tp = entry + sl_pips * pip, entry - tp_pips * pip
                # Három vízszintes trendvonal a belépőre CENTRÁLVA: −3…+3 M1
                # gyertya → 6 hosszú. TP zöld (fent BUY-nál), belépő NARANCS az
                # entry árszintjén, SL piros. + FÜGGŐLEGES irány-jelzés a belépő
                # idejénél: zöld BUY / piros SELL.
                t0    = t - 3 * 60
                t_end = t + 3 * 60
                objects.append(viz.VLine(
                    name=f"m1sig_{t}", t1=t,
                    color="green" if sig == "BUY" else "red", width=2))
                objects.append(viz.Trend(name=f"m1entry_{t}", t1=t0, p1=entry, t2=t_end, p2=entry,
                                         color="orange", width=2))
                objects.append(viz.Trend(name=f"tp_{t}", t1=t0, p1=tp, t2=t_end, p2=tp,
                                         color="green", width=2))
                objects.append(viz.Trend(name=f"sl_{t}", t1=t0, p1=sl, t2=t_end, p2=sl,
                                         color="red", width=2))

        # ── Beállítás-táblázat (bal-felső sarok) ───────────────────────────
        rows = [
            ("tbl_title", "wpr_sma", 20),
            ("tbl_sma", f"SMA Period: {md.params.get('sma_period', '?')}", 36),
            ("tbl_w15", f"WPR M15: {md.params.get('wpr_m15_period', '?')}", 52),
            ("tbl_w1",  f"WPR M1: {md.params.get('wpr_m1_period', '?')}", 68),
        ]
        for name, text, y in rows:
            objects.append(viz.Label(name=name, text=text, corner=0, x=10, y=y,
                                     color="black", fontsize=9))

        return objects

    # --- Optimalizálás ----------------------------------------------------

    def base_params(self, cfg: dict) -> dict:
        return {**cfg.get("indicators", {}), **cfg.get("sltp", {}),
                **cfg.get("position_mgmt", {})}

    def param_space(self, cfg: dict, base_params: dict, method: str,
                    max_trials: int) -> list[dict]:
        from ml.optimizer import generate_random_params, generate_grid_params
        opt_cfg = cfg["optimizer"]
        if method == "grid":
            return generate_grid_params(opt_cfg, base_params, self.constraints_ok)
        return generate_random_params(opt_cfg, base_params, max_trials,
                                      self.constraints_ok)

    def constraints_ok(self, params: dict) -> bool:
        """WPR szint-sorrend (sell_extreme > trigger > buy_extreme) + session."""
        p = params
        if p.get("wpr_m15_sell_extreme", -20) <= p.get("wpr_m15_trigger", -50):
            return False
        if p.get("wpr_m15_trigger", -50) <= p.get("wpr_m15_buy_extreme", -80):
            return False
        if p.get("wpr_m1_sell_extreme", -20) <= p.get("wpr_m1_trigger", -50):
            return False
        if p.get("wpr_m1_trigger", -50) <= p.get("wpr_m1_buy_extreme", -80):
            return False
        if p.get("trade_hour_start", 0) >= p.get("trade_hour_end", 24):
            return False
        return True

    # --- Backtest-motor hookok (a core signal/indicator becsomagolva) ------

    def bt_indicators(self, df_hi, df_lo, params):
        return compute_indicators(df_hi, df_lo, params)

    def bt_warmup(self, params: dict, timeframe_label: str) -> int:
        if timeframe_label == "M15":
            return max(params["sma_period"], params["wpr_m15_period"],
                       params["atr_period"])
        return params["wpr_m1_period"]

    def bt_new_state(self, symbol: str) -> PairState:
        return PairState(symbol)

    def bt_on_high_close(self, state, hi_row, params):
        # A jelzésmatek NaN-mentes zárt gyertyát vár (a warmup-szeletelés után
        # ez teljesül) — az őrfeltétel a régi run_pair-rel bitre azonos viselkedés.
        if pd.isna(hi_row["sma"]) or pd.isna(hi_row["wpr"]) or pd.isna(hi_row["atr"]):
            return state
        return check_m15_signal(state, close=float(hi_row["close"]),
                                sma=float(hi_row["sma"]),
                                wpr_m15=float(hi_row["wpr"]), params=params)

    def bt_on_low_close(self, state, prev_lo_row, lo_row, params) -> str:
        if prev_lo_row is None:
            return "NONE"
        prev_wpr, cur_wpr = prev_lo_row["wpr"], lo_row["wpr"]
        if pd.isna(prev_wpr) or pd.isna(cur_wpr):
            return "NONE"
        return check_m1_entry(state, float(prev_wpr), float(cur_wpr), params)
