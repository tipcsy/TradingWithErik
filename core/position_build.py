"""Pozícióépítés (ráépítés) — tiszta logika (nincs MT5/tkinter függés).

Forrás-tananyag: Obsidian `Tananyagok/Pozícióépítés.md`. Elv:
  • Gyertyás építés-jel (a doc kedvenc technikája): akkor építs, ha egy ZÁRT gyertya
    FELJEBB (BUY) / LEJJEBB (SELL) zár, mint az előző ráépítés referenciája.
  • Piramidális méret: minden ráépítés az előző × `size_factor` (csökkenő), min_lot-ig.
  • 1. szabály (kockázatmentesség): a hívó az ÖSSZES azonos-szimbólumú stopot az
    ÁTLAGÁRRA húzza — így a legrosszabb esetben is ~nulla az eredmény. Ezt a live/GUI
    réteg végzi (MT5), ez a modul csak a jelet + a méretet + az átlagárat számolja.

A „suitable" (alkalmas) állapot = a pozíció KOCKÁZATMENTES (a hívó tudja) ÉS a
`build_signal` szól. Az `enabled`-et a mód (off|manual|auto) dönti a hívónál.
"""

from __future__ import annotations

import math

MODE_OFF = "off"
MODE_MANUAL = "manual"
MODE_AUTO = "auto"          # későbbi kör (automatikus építés minden jel-gyertyán)
MODES = (MODE_OFF, MODE_MANUAL, MODE_AUTO)


def default_config() -> dict:
    """Per-instrumentum építés-beállítás alap (a per-pár állapot felülírja)."""
    return {
        "mode":        MODE_OFF,
        "size_factor": 0.7,     # piramidális: minden add az előző × faktor
        "timeframe":   "M15",   # mely időkeret ZÁRT gyertyáin figyeljük a jelet
    }


def build_signal(bars, direction: str, ref_close: float) -> bool:
    """Építés-jel az UTOLSÓ ZÁRT gyertyán: BUY-nál a záró > referencia (új csúcs-zárás),
    SELL-nél a záró < referencia. `ref_close` = az előző ráépítés (vagy az első
    ráépítésnél a belépő) gyertyájának záróára. A formálódó gyertya az utolsó sor."""
    if bars is None or len(bars) < 2 or ref_close is None:
        return False
    try:
        c = float(bars["close"].iloc[-2])
    except Exception:
        return False
    if math.isnan(c):
        return False
    if direction == "BUY":
        return c > float(ref_close)
    if direction == "SELL":
        return c < float(ref_close)
    return False


def next_lot(last_lot: float, size_factor: float, min_lot: float, lot_step: float) -> float:
    """A következő ráépítés mérete (piramidális, csökkenő). A lot_step-re illesztve,
    min_lot alá nem megy. Ha már a min_lot-on vagyunk → tovább a min_lot-tal (a doc:
    a végén a legkisebbel pakol tovább)."""
    if last_lot <= 0 or min_lot <= 0 or lot_step <= 0:
        return 0.0
    want = last_lot * float(size_factor)
    stepped = math.floor(want / lot_step + 1e-9) * lot_step
    return max(min_lot, round(stepped, 8))


def average_price(positions) -> float:
    """Volumen-súlyozott átlagár (a null pont) az azonos-szimbólum+irány nyitott
    pozíciókból. `positions`: [(price_open, volume), …]. 0.0, ha nincs volumen."""
    tot_v = sum(float(v) for _, v in positions)
    if tot_v <= 0:
        return 0.0
    return sum(float(p) * float(v) for p, v in positions) / tot_v
