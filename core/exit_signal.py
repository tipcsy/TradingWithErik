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

from core.indicator_engine import supertrend, wpr as _wpr, sma as _sma

INDICATOR_SUPERTREND = "supertrend"
INDICATOR_WPR = "wpr"
INDICATORS = (INDICATOR_SUPERTREND, INDICATOR_WPR)


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
    return False
