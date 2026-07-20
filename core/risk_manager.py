"""
Portfólió szintű kockázatkezelés.

Alapelv: account × risk_pct = az összes slot EGYÜTTES kockázata.
  - Normál eset: lot = (teljes_cél / max_slots) / (sl_pips × pip_value)  → FLOOR-ra kerekítve
  - Kis számla (min_lot kényszer): effective_slots = ROUND(cél / tényleges_kockázat × max_slots)
"""

import math


def calc_sl_tp_pips(atr_value: float, params: dict) -> tuple[float, float]:
    """SL és TP pip értéke ATR alapján."""
    sl_pips = atr_value / params.get("pip_size", 0.0001) * params["sl_atr_mult"]
    tp_pips = sl_pips * params["tp_rr_ratio"]
    return sl_pips, tp_pips


def calc_lot(
    balance: float,
    sl_pips: float,
    pair_cfg: dict,
    trading_cfg: dict,
    effective_slots: int,
) -> float:
    """
    Lot méret számítása egy slothoz.
    Mindig FLOOR-ra kerekít (soha nem lép túl a kockázaton lot oldalon).
    """
    risk_pct      = trading_cfg["account_risk_pct"]
    total_risk    = balance * risk_pct
    risk_per_slot = total_risk / effective_slots

    pip_value  = pair_cfg["pv1_usd"]   # 1 lot, 1 pip mozgás USD értéke
    # min_lot/lot_step hiányozhat (pl. GUI-ból hozzáadott vagy hiányos config) →
    # biztonságos alapérték, hogy az optimalizálás/backteszt ne szálljon el csendben.
    lot_step   = pair_cfg.get("lot_step", 0.01)
    min_lot    = pair_cfg.get("min_lot", 0.01)

    if sl_pips <= 0 or pip_value <= 0:
        return min_lot

    raw_lot = risk_per_slot / (sl_pips * pip_value)
    lot = math.floor(raw_lot / lot_step) * lot_step
    return max(lot, min_lot)


def calc_effective_slots(
    balance: float,
    sl_pips: float,
    pair_cfg: dict,
    trading_cfg: dict,
) -> int:
    """
    Ha a min_lot kockázat meghaladja a cél kockázatot slotanként,
    csökkenti az elérhető slotok számát arányosan (ROUND, min 1).
    """
    max_slots  = trading_cfg["max_open_slots"]
    risk_pct   = trading_cfg["account_risk_pct"]
    total_risk = balance * risk_pct

    pip_value = pair_cfg["pv1_usd"]
    min_lot   = pair_cfg.get("min_lot", 0.01)

    actual_risk = min_lot * sl_pips * pip_value

    if actual_risk <= 0:
        return max_slots

    slots = round(total_risk / actual_risk * max_slots)
    return max(1, min(slots, max_slots))


class SlotManager:
    """
    Globális slot kezelés: nyomon követi a nyitott és kockázatmentes pozíciókat.
    """

    def __init__(self, max_slots: int):
        self.max_slots = max_slots
        self._positions: dict[int, bool] = {}  # ticket → risk_free

    def occupied(self) -> int:
        """Valóban foglalt (nem kockázatmentes) slotok száma."""
        return sum(1 for rf in self._positions.values() if not rf)

    def free(self) -> int:
        return self.max_slots - self.occupied()

    def can_open(self) -> bool:
        return self.free() > 0

    def add(self, ticket: int):
        self._positions[ticket] = False

    def ensure(self, ticket: int) -> bool:
        """Nyomon követésbe vétel, ha még nem ismert — a MEGLÉVŐ kockázatmentes
        jelölést nem írja felül (ellentétben az `add`-del). Az utólag stratégiához
        rendelt (kézzel nyitott) pozíciók így nem maradnak ki a slot-számlálásból.
        True, ha most került be."""
        if ticket in self._positions:
            return False
        self._positions[ticket] = False
        return True

    def set_risk_free(self, ticket: int):
        if ticket in self._positions:
            self._positions[ticket] = True

    def remove(self, ticket: int):
        self._positions.pop(ticket, None)

    def is_risk_free(self, ticket: int) -> bool:
        return self._positions.get(ticket, False)

    def all_tickets(self) -> list[int]:
        return list(self._positions.keys())
