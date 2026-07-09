"""
Per-pár kockázatcsökkentő PRESET állapot — perzisztens JSON-ban.

A felületen (instrument-ablak / Live sor) instrumentumonként választható a
kockázatcsökkentő preset (off|risky|halving/Felező|shield/Pajzs). Ez a modul
tárolja/olvassa (`data/risk_mode.json`), mint a `risky_mode.py` a risky-t.

A régi `risky_mode` (R gomb, kívülről is írható fájl) MEGMARAD: az `effective`
egyesíti — ha itt nincs kifejezett preset (off) DE a risky_mode be van kapcsolva,
akkor 'risky'. Így az R gomb továbbra is működik, a preset-választás felülírja.
"""

import json
import threading
from pathlib import Path

from core import risky_mode
from core.risk_reduction import (
    PRESET_OFF, PRESET_RISKY, PRESET_HALVING, PRESET_SHIELD, PRESETS,
)

PATH = Path(__file__).resolve().parents[1] / "data" / "risk_mode.json"

# Sorrend a felületi „körbe-váltáshoz" (R gomb / kattintás)
CYCLE = (PRESET_OFF, PRESET_RISKY, PRESET_HALVING, PRESET_SHIELD)
# Rövid felirat + szín-név a soron/gombon
LABEL = {PRESET_OFF: "—", PRESET_RISKY: "R", PRESET_HALVING: "F", PRESET_SHIELD: "P"}
NAME  = {PRESET_OFF: "Ki", PRESET_RISKY: "Risky", PRESET_HALVING: "Felező",
         PRESET_SHIELD: "Pajzs"}

_lock = threading.Lock()
_state: dict[str, str] = {}


def load() -> dict:
    with _lock:
        try:
            if PATH.exists():
                with open(PATH, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    _state.clear()
                    _state.update({str(k): str(v) for k, v in data.items()
                                   if str(v) in PRESETS})
        except Exception:
            pass
        return dict(_state)


def get_preset(symbol: str) -> str:
    with _lock:
        return _state.get(symbol, PRESET_OFF)


def set_preset(symbol: str, preset: str):
    if preset not in PRESETS:
        preset = PRESET_OFF
    with _lock:
        _state[symbol] = preset
        _save_locked()


def cycle_preset(symbol: str) -> str:
    with _lock:
        cur = _state.get(symbol, PRESET_OFF)
        nxt = CYCLE[(CYCLE.index(cur) + 1) % len(CYCLE)] if cur in CYCLE else PRESET_RISKY
        _state[symbol] = nxt
        _save_locked()
        return nxt


def effective_preset(symbol: str) -> str:
    """A ténylegesen érvényes preset: a per-pár választás, DE ha az 'off' és a
    régi risky_mode (R gomb / külső fájl) be van kapcsolva → 'risky'."""
    p = get_preset(symbol)
    if p == PRESET_OFF and risky_mode.is_risky(symbol):
        return PRESET_RISKY
    return p


def all_states() -> dict:
    with _lock:
        return dict(_state)


def _save_locked():
    try:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_state, f, indent=2, ensure_ascii=False)
        tmp.replace(PATH)
    except Exception:
        pass
