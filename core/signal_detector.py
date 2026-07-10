"""
Jelzés detektálás logika.

M15 „jó zóna" (jelzési ablak) — a WPR_SMA Stratégia jegyzet szerint:

SELL (close < SMA):
  • NYÍLIK: a WPR a felső extrémből (>= sell_extreme, pl. -20) indul és LEFELÉ
    átüti a SELL triggert (<= sell_trigger, pl. -50) → jó zóna ON.
  • ZÁRUL két módon:
      1. érvénytelenítés — a WPR VISSZAMEGY a kiinduló (felső) extrémbe
         (>= sell_extreme);
      2. kifutás — a WPR eléri a MÁSIK (alsó) extrémet (<= buy_extreme), majd
         visszajön és FELFELÉ átüti a triggert (>= sell_trigger).
    FONTOS: a trigger puszta visszaütése önmagában NEM zár (a régi logikával
    ellentétben) — csak ha előbb elértük az alsó extrémet, vagy visszaértünk a
    felső extrémbe.

BUY (close > SMA): tükörkép — alsó extrémből (<= buy_extreme) FELFELÉ átüti a BUY
  triggert (>= buy_trigger); zárul, ha vissza az alsó extrémbe, vagy a felső
  extrém után lefelé átüti a triggert.

A BUY és SELL trigger KÜLÖN paraméter (wpr_m15_buy_trigger / wpr_m15_sell_trigger;
visszafelé kompatibilis fallback a régi közös wpr_m15_trigger-re).

M1 belépő (VÁLTOZATLAN): ha a jó zóna nyitva, a WPR az M1 extrémből a triggerbe zár.
"""

from dataclasses import dataclass, field
from typing import Literal

Direction = Literal["BUY", "SELL", "NONE"]


@dataclass
class PairState:
    symbol: str
    direction: Direction = "NONE"    # SMA alapú trend irány
    m15_window_open: bool = False    # Aktív M15 jelzési ablak (jó zóna)
    m15_extreme_seen: bool = False   # A KIINDULÓ extrémben volt-e (felfegyverzés)
    m15_opposite_seen: bool = False  # A jó zónában elérte-e a MÁSIK extrémet (kifutás)


def check_m15_signal(
    state: PairState,
    close: float,
    sma: float,
    wpr_m15: float,
    params: dict,
) -> PairState:
    """
    M15 gyertya zárásakor hívandó. Frissíti a trend irányt és a „jó zóna" (jelzési
    ablak) állapotgépét a modul-docstringben leírt szabályok szerint.
    """
    sell_extreme = params["wpr_m15_sell_extreme"]          # felső extrém (pl. -20)
    buy_extreme  = params["wpr_m15_buy_extreme"]           # alsó extrém (pl. -80)
    _trig        = params.get("wpr_m15_trigger", -50)      # régi közös (fallback)
    sell_trigger = params.get("wpr_m15_sell_trigger", _trig)
    buy_trigger  = params.get("wpr_m15_buy_trigger",  _trig)

    # Trend irány az SMA alapján
    if close < sma:
        new_dir = "SELL"
    elif close > sma:
        new_dir = "BUY"
    else:
        new_dir = "NONE"

    # Irányváltáskor a jó zóna nullázódik (egy zóna egy irányhoz tartozik).
    if new_dir != state.direction:
        state.m15_window_open = False
        state.m15_extreme_seen = False
        state.m15_opposite_seen = False
    state.direction = new_dir

    if new_dir == "SELL":
        # (a) kiinduló (felső) extrém: felfegyverez; nyitott zónát ÉRVÉNYTELENÍT (1.)
        if wpr_m15 >= sell_extreme:
            state.m15_extreme_seen = True
            if state.m15_window_open:
                state.m15_window_open = False
                state.m15_opposite_seen = False
        # (b) másik (alsó) extrém elérése a nyitott zónában
        if state.m15_window_open and wpr_m15 <= buy_extreme:
            state.m15_opposite_seen = True
        # (c) KIFUTÁS (2.): alsó extrém után a trigger FELFELÉ visszaütése
        if state.m15_window_open and state.m15_opposite_seen and wpr_m15 >= sell_trigger:
            state.m15_window_open = False
            state.m15_extreme_seen = False
            state.m15_opposite_seen = False
        # (d) NYITÁS: felfegyverzett + a trigger LEFELÉ átütése
        if (not state.m15_window_open) and state.m15_extreme_seen and wpr_m15 <= sell_trigger:
            state.m15_window_open = True
            state.m15_opposite_seen = False

    elif new_dir == "BUY":
        # (a) kiinduló (alsó) extrém: felfegyverez; nyitott zónát ÉRVÉNYTELENÍT (1.)
        if wpr_m15 <= buy_extreme:
            state.m15_extreme_seen = True
            if state.m15_window_open:
                state.m15_window_open = False
                state.m15_opposite_seen = False
        # (b) másik (felső) extrém elérése a nyitott zónában
        if state.m15_window_open and wpr_m15 >= sell_extreme:
            state.m15_opposite_seen = True
        # (c) KIFUTÁS (2.): felső extrém után a trigger LEFELÉ visszaütése
        if state.m15_window_open and state.m15_opposite_seen and wpr_m15 <= buy_trigger:
            state.m15_window_open = False
            state.m15_extreme_seen = False
            state.m15_opposite_seen = False
        # (d) NYITÁS: felfegyverzett + a trigger FELFELÉ átütése
        if (not state.m15_window_open) and state.m15_extreme_seen and wpr_m15 >= buy_trigger:
            state.m15_window_open = True
            state.m15_opposite_seen = False
    else:
        state.m15_window_open = False
        state.m15_extreme_seen = False
        state.m15_opposite_seen = False

    return state


def check_m1_entry(
    state: PairState,
    wpr_m1_prev: float,
    wpr_m1_close: float,
    params: dict,
) -> Direction:
    """
    M1 gyertya zárásakor hívandó.
    Ha az M15 jelzési ablak nyitva van, ellenőrzi az M1 belépési feltételt.
    Visszatér: "BUY" | "SELL" | "NONE"
    """
    if not state.m15_window_open:
        return "NONE"

    m1_sell_extreme = params["wpr_m1_sell_extreme"]  # pl. -20
    m1_buy_extreme  = params["wpr_m1_buy_extreme"]   # pl. -80
    m1_trigger      = params["wpr_m1_trigger"]       # pl. -50

    if state.direction == "SELL":
        # M1 WPR volt >= m1_sell_extreme (extrém), majd zárt <= m1_trigger
        if wpr_m1_prev >= m1_sell_extreme and wpr_m1_close <= m1_trigger:
            return "SELL"

    elif state.direction == "BUY":
        # M1 WPR volt <= m1_buy_extreme (extrém), majd zárt >= m1_trigger
        if wpr_m1_prev <= m1_buy_extreme and wpr_m1_close >= m1_trigger:
            return "BUY"

    return "NONE"
