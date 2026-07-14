"""Kiszállási (lezárási) jelzések — a nyitott pozíció zárása indikátor-alapon.

A tananyag („Kiszállási jelzések", lásd Obsidian: Tananyagok/Kiszállási jelzések.md)
lényege: egy VIRTUÁLIS célár UTÁN figyeljük a kiszállási jelet, és akkor zárunk,
amikor egy gyertya „átzárja az indikátor vonalát". Ez a modul a **jelet** adja
(tiszta logika, nincs MT5/tkinter függés); a *virtuális célár* kapuzása (mikortól
figyeljük) és a tényleges zárás a HÍVÓ (live_trader / backtest) dolga.

Használat: a kockázatcsökkentő runner (Pajzs/Felező maradéka) TP nélkül fut — ez a
modul mondja meg, mikor zárja le a motor. Egyelőre két determinista jel:
  • Supertrend-flip: a Supertrend iránya a pozícióval SZEMBE fordul,
  • WPR-visszazárás: a WPR a mozgóátlagát a pozícióval szembe keresztezi.
A divergencia (a tananyag szerint a legerősebb) egy későbbi kör.
"""

from __future__ import annotations

import math

import numpy as np

from core.indicator_engine import (
    supertrend, wpr as _wpr, sma as _sma, rsi as _rsi, cci as _cci,
)

INDICATOR_SUPERTREND = "supertrend"
INDICATOR_WPR = "wpr"
INDICATOR_DIVERGENCE = "divergence"
INDICATORS = (INDICATOR_SUPERTREND, INDICATOR_WPR, INDICATOR_DIVERGENCE)

OSC_RSI = "rsi"
OSC_CCI = "cci"
OSCILLATORS = (OSC_RSI, OSC_CCI)


def default_config() -> dict:
    """Egy kiszállási-modul alap-beállítása (a per-pár állapot/optimalizáló
    felülírja). `enabled=False` → a modul nem szól bele (visszafelé kompatibilis)."""
    return {
        "enabled":       False,
        "indicator":     INDICATOR_SUPERTREND,
        "timeframe":     "M15",       # mely időkeret ZÁRT gyertyáin figyelünk
        # Supertrend (a tananyag ajánlása: 10 / 1.7 — kicsit korábbi, kedvezőbb kiszállás)
        "st_period":     10,
        "st_multiplier": 1.7,
        # WPR + mozgóátlag (átzárás a MA-n)
        "wpr_period":    20,
        "wpr_ma_period": 100,
        # Divergencia (a tananyag szerint a legerősebb): RSI/CCI oszcillátor +
        # pivot-alapú divergencia + a középvonal (RSI 50 / CCI 0) átzárása.
        "osc":           OSC_RSI,     # rsi | cci
        "div_period":    14,
        "div_pivot":     5,           # pivot félszélesség (±gyertya) a csúcsokhoz
    }


def _dir_sign(direction: str) -> int:
    return 1 if direction == "BUY" else -1 if direction == "SELL" else 0


def supertrend_exit(bars, direction: str, period: int, multiplier: float) -> bool:
    """Kiszállás, ha a Supertrend iránya az UTOLSÓ ZÁRT gyertyán a pozícióval
    SZEMBE mutat (long-nál -1 / csökkenő, short-nál +1 / emelkedő). A zárt gyertya
    az utolsó előtti sor (az utolsó formálódik)."""
    sign = _dir_sign(direction)
    if sign == 0 or bars is None or len(bars) < period + 3:
        return False
    _line, st_dir = supertrend(bars["high"], bars["low"], bars["close"],
                               period=period, multiplier=multiplier)
    d = st_dir.iloc[-2]                 # utolsó ZÁRT gyertya iránya
    if d == 0 or (isinstance(d, float) and math.isnan(d)):
        return False
    # long (sign=+1) → kiszállás, ha ST csökkenő (-1); short → ha ST emelkedő (+1)
    return bool(int(d) == -sign)


def wpr_exit(bars, direction: str, period: int, ma_period: int) -> bool:
    """Kiszállás, ha a WPR az utolsó ZÁRT gyertyán a mozgóátlagát a pozícióval
    SZEMBE KERESZTEZI (long-nál lefelé, short-nál fölfelé) — „a gyertya átzárja a
    vonalat" esemény (az előző zárt gyertyán még a jó oldalon volt)."""
    sign = _dir_sign(direction)
    if sign == 0 or bars is None or len(bars) < period + ma_period + 3:
        return False
    w = _wpr(bars["high"], bars["low"], bars["close"], period)
    m = _sma(w, ma_period)
    w_prev, w_cur = w.iloc[-3], w.iloc[-2]        # két utolsó ZÁRT gyertya
    m_prev, m_cur = m.iloc[-3], m.iloc[-2]
    if any(x is None or (isinstance(x, float) and math.isnan(x))
           for x in (w_prev, w_cur, m_prev, m_cur)):
        return False
    if sign > 0:      # long → WPR lefelé keresztezi a MA-t
        return bool(w_prev >= m_prev and w_cur < m_cur)
    else:             # short → WPR fölfelé keresztezi a MA-t
        return bool(w_prev <= m_prev and w_cur > m_cur)


def _osc_series(bars, osc: str, period: int):
    """Az oszcillátor sorozata + a KÖZÉPVONAL (RSI→50, CCI→0)."""
    if osc == OSC_CCI:
        return _cci(bars["high"], bars["low"], bars["close"], period).to_numpy(), 0.0
    return _rsi(bars["close"], period).to_numpy(), 50.0


def divergence_exit_series(bars, direction: str, osc: str = OSC_RSI,
                           period: int = 14, pivot: int = 5, max_gap: int = 60):
    """Divergencia-alapú kiszállás — gyertyánkénti bool tömb (a live és a backtest is
    ezt használja). Long-nál a BEARISH divergenciát figyeli (ár magasabb csúcs, de az
    oszcillátor alacsonyabb csúcs a két legutóbbi MEGERŐSÍTETT pivot-csúcson), és a
    jel akkor SZÓL, amikor az oszcillátor a KÖZÉPVONALAT lefelé keresztezi (a tananyag
    „a gyertya átzárja a vonalat" szabálya). Short-nál a tükörkép (bullish divergencia
    + fölfelé keresztezés). Egy pivot az i-edik gyertyán az (i+pivot)-edik gyertyán
    válik megerősítetté (nincs look-ahead). `max_gap`: hány gyertyáig érvényes még a
    divergencia a második pivot után."""
    sign = _dir_sign(direction)
    n = len(bars)
    out = np.zeros(n, dtype=bool)
    if sign == 0 or n < period + 2 * pivot + 3:
        return out
    o, mid = _osc_series(bars, osc, period)
    h = bars["high"].to_numpy()
    l = bars["low"].to_numpy()
    piv: list = []                       # (idx, ár, oszc) — long: csúcsok, short: mélyek
    for nb in range(n):
        i = nb - pivot                   # az i-edik pivot most (nb-nél) erősödik meg
        if i >= pivot and not np.isnan(o[i]):
            is_piv = (h[i] == h[i - pivot:i + pivot + 1].max()) if sign > 0 \
                else (l[i] == l[i - pivot:i + pivot + 1].min())
            if is_piv:
                cand = (i, (h[i] if sign > 0 else l[i]), o[i])
                # Közeli (plató/döntetlen) pivotok dedupolása: ha az előzőhöz
                # `pivot`-nál közelebb van, csak akkor cseréljük, ha SZÉLSŐSÉGESEBB;
                # különben ÚJ pivotként vesszük fel. Így két külön csúcs marad külön.
                if piv and (i - piv[-1][0]) <= pivot:
                    if (sign > 0 and cand[1] > piv[-1][1]) or (sign < 0 and cand[1] < piv[-1][1]):
                        piv[-1] = cand
                else:
                    piv.append(cand)
        if nb < 1 or len(piv) < 2 or np.isnan(o[nb]) or np.isnan(o[nb - 1]):
            continue
        (i1, p1, o1) = piv[-2]
        (i2, p2, o2) = piv[-1]
        if (nb - i2) > max_gap:
            continue
        if sign > 0:
            div = (p2 > p1) and (o2 < o1)               # magasabb ár-csúcs, alacsonyabb oszc-csúcs
            crossed = (o[nb - 1] >= mid and o[nb] < mid)  # középvonal lefelé
        else:
            div = (p2 < p1) and (o2 > o1)               # alacsonyabb ár-mély, magasabb oszc-mély
            crossed = (o[nb - 1] <= mid and o[nb] > mid)  # középvonal fölfelé
        out[nb] = bool(div and crossed)
    return out


def divergence_exit(bars, direction: str, osc: str, period: int, pivot: int) -> bool:
    """Divergencia-kiszállás az UTOLSÓ ZÁRT gyertyán (a formálódó az utolsó sor)."""
    if bars is None:
        return False
    s = divergence_exit_series(bars, direction, osc, period, pivot)
    return bool(s[-2]) if len(s) >= 2 else False


def exit_triggered(bars, direction: str, cfg: dict) -> bool:
    """A kiválasztott kiszállási indikátor jele az utolsó ZÁRT gyertyán.
    `cfg` a `default_config()` szerinti (a hívó tölti a per-pár beállításból).
    `enabled=False` → mindig False. Ismeretlen indikátor → False."""
    if not cfg or not cfg.get("enabled"):
        return False
    ind = cfg.get("indicator", INDICATOR_SUPERTREND)
    if ind == INDICATOR_SUPERTREND:
        return supertrend_exit(bars, direction,
                               int(cfg.get("st_period", 10)),
                               float(cfg.get("st_multiplier", 1.7)))
    if ind == INDICATOR_WPR:
        return wpr_exit(bars, direction,
                        int(cfg.get("wpr_period", 20)),
                        int(cfg.get("wpr_ma_period", 100)))
    if ind == INDICATOR_DIVERGENCE:
        return divergence_exit(bars, direction,
                               cfg.get("osc", OSC_RSI),
                               int(cfg.get("div_period", 14)),
                               int(cfg.get("div_pivot", 5)))
    return False
