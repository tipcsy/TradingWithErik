"""
Jelzés detektálás logika:

SELL:
  1. M15: close < SMA  (árfolyam SMA alatt)
  2. M15: WPR indult <= sell_extreme, majd átütötte a trigger szintet lefelé
     → jelzési ablak nyílik
  3. M1: WPR volt >= m1_sell_extreme, majd zárt <= m1_trigger
     → SELL belépési jel
  4. Jelzési ablak zárul, ha M15 WPR visszamegy >= trigger fölé

BUY: tükörképe a SELL-nek.
"""

from dataclasses import dataclass, field
from typing import Literal

Direction = Literal["BUY", "SELL", "NONE"]


@dataclass
class PairState:
    symbol: str
    direction: Direction = "NONE"   # SMA alapú trend irány
    m15_window_open: bool = False   # Aktív M15 jelzési ablak
    m15_extreme_seen: bool = False  # Volt-e már extrém zónában az M15 WPR


def check_m15_signal(
    state: PairState,
    close: float,
    sma: float,
    wpr_m15: float,
    params: dict,
) -> PairState:
    """
    M15 gyertya zárásakor hívandó.
    Frissíti a trend irányt és a jelzési ablakot.
    """
    sell_extreme = params["wpr_m15_sell_extreme"]   # pl. -20
    buy_extreme  = params["wpr_m15_buy_extreme"]    # pl. -80
    trigger      = params["wpr_m15_trigger"]        # pl. -50

    # Trend irány az SMA alapján
    if close < sma:
        state.direction = "SELL"
    elif close > sma:
        state.direction = "BUY"
    else:
        state.direction = "NONE"

    if state.direction == "SELL":
        # Extrém zóna elérése (WPR >= sell_extreme, pl. -20)
        if wpr_m15 >= sell_extreme:
            state.m15_extreme_seen = True

        if state.m15_extreme_seen:
            if wpr_m15 <= trigger:
                # WPR átütötte a triggert lefelé → ablak nyílik
                state.m15_window_open = True
            elif wpr_m15 >= trigger and state.m15_window_open:
                # WPR visszament trigger fölé → ablak zárul
                state.m15_window_open = False
                state.m15_extreme_seen = False

    elif state.direction == "BUY":
        # Extrém zóna elérése (WPR <= buy_extreme, pl. -80)
        if wpr_m15 <= buy_extreme:
            state.m15_extreme_seen = True

        if state.m15_extreme_seen:
            if wpr_m15 >= trigger:
                # WPR átütötte a triggert felfelé → ablak nyílik
                state.m15_window_open = True
            elif wpr_m15 <= trigger and state.m15_window_open:
                # WPR visszament trigger alá → ablak zárul
                state.m15_window_open = False
                state.m15_extreme_seen = False
    else:
        state.m15_window_open = False
        state.m15_extreme_seen = False

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
