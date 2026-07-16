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

# ── Ráépítés-triggerek (mikor jöjjön a következő adalék) ─────────────────────
TRIGGER_CANDLE     = "candle"      # gyertyás trendkövető (új csúcs/mély-zárás a ref fölött)
TRIGGER_R_FIXED    = "r_fixed"     # fix R-rács: +1R, +2R, +3R… (r_step állandó)
TRIGGER_R_CONVERGE = "r_converge"  # R-felező: a lépés zsugorodik (1R, +0.5R, +0.25R…) → sűrűsödik
TRIGGERS = (TRIGGER_CANDLE, TRIGGER_R_FIXED, TRIGGER_R_CONVERGE)
TRIGGER_NAME = {TRIGGER_CANDLE: "Gyertyás", TRIGGER_R_FIXED: "Fix R",
                TRIGGER_R_CONVERGE: "R-felező"}

# Biztonsági plafon a ráépítések számára. Nincs FELHASZNÁLÓI korlát (mehet sok R-ig),
# de az R-FELEZŐ a konvergencia-plafon átlépésekor VÉGTELEN adalékot nyitna (minden
# R-szint a plafon alatt van) → ez a hard-cap ezt fogja meg. Nagy, hogy normál
# használatnál gyakorlatilag ne érződjön (a Fix R / gyertyás amúgy is self-limitál).
HARD_MAX_ADDS = 100


def default_config() -> dict:
    """Per-instrumentum építés-beállítás alap (a per-pár állapot felülírja)."""
    return {
        "mode":        MODE_OFF,
        "size_factor": 0.7,     # piramidális: minden add az előző × faktor
        "timeframe":   "M15",   # mely időkeret ZÁRT gyertyáin figyeljük a jelet
        "trigger":     TRIGGER_CANDLE,   # gyertyás | r_fixed | r_converge
        "r_step":      1.0,     # R-alapú triggernél az (első) lépés R-ben
        "r_shrink":    0.5,     # R-felezőnél a lépés szorzója add-onként (0.5 = felező)
    }


def r_level(initial_entry, r_price, direction: str, n_add: int, cfg: dict):
    """Az `n_add`-adik ráépítés (n_add ≥ 1) ÁRSZINTJE az R-alapú triggereknél. Az R a
    csomag kockázati egysége (az INDULÓ láb |belépő − eredeti SL|-je). None, ha nem
    R-alapú a trigger vagy nincs érvényes R. A szintek az INDULÓ belépőtől mérve:
      • Fix R:     init ± n·r_step·R
      • R-felező:  init ± r_step·(1 − shrink^n)/(1 − shrink)·R  (konvergál → sűrűsödik)."""
    if initial_entry is None or not r_price or r_price <= 0 or n_add < 1:
        return None
    step = float(cfg.get("r_step", 1.0))
    trig = cfg.get("trigger", TRIGGER_CANDLE)
    if trig == TRIGGER_R_FIXED:
        cum = n_add * step
    elif trig == TRIGGER_R_CONVERGE:
        shrink = float(cfg.get("r_shrink", 0.5))
        if abs(1.0 - shrink) < 1e-9:
            cum = n_add * step
        else:
            cum = step * (1.0 - shrink ** n_add) / (1.0 - shrink)
    else:
        return None
    return (initial_entry + cum * r_price) if direction == "BUY" \
        else (initial_entry - cum * r_price)


def build_signal(bars, direction: str, ref_close: float) -> bool:
    """GYERTYÁS építés-jel az UTOLSÓ ZÁRT gyertyán: BUY-nál a záró > referencia (új
    csúcs-zárás), SELL-nél a záró < referencia. `ref_close` = az előző ráépítés (vagy az
    elsőnél a belépő) záróára. A formálódó gyertya az utolsó sor."""
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


def build_fires(bars, direction: str, cfg: dict, *, ref_close=None,
                initial_entry=None, r_price=None, n_add: int = 1) -> bool:
    """EGYSÉGES ráépítés-jel az UTOLSÓ ZÁRT gyertyán, a `cfg['trigger']` szerint:
      • candle    → a `build_signal` (gyertyás trendkövető).
      • r_fixed / r_converge → a záró elérte-e az `n_add`-adik R-szintet.
    Determinisztikus (árszint / gyertyazáró) → backtestelhető Kéziben is."""
    trig = cfg.get("trigger", TRIGGER_CANDLE)
    if trig == TRIGGER_CANDLE:
        return build_signal(bars, direction, ref_close)
    if n_add > HARD_MAX_ADDS:          # biztonsági plafon (fő cél: R-felező konvergencia)
        return False
    if bars is None or len(bars) < 2:
        return False
    try:
        c = float(bars["close"].iloc[-2])
    except Exception:
        return False
    if math.isnan(c):
        return False
    lvl = r_level(initial_entry, r_price, direction, n_add, cfg)
    if lvl is None:
        return False
    return c >= lvl if direction == "BUY" else c <= lvl


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
