"""
Per-pár kockázatcsökkentő ÁLLAPOT — perzisztens JSON-ban (`data/risk_mode.json`).

Instrumentumonként: PRESET (off|risky|halving/Felező|shield/Pajzs) + haladó
felülbírálások: `cautious` (óvatos/felezett belépő-méret, None=preset szerint) és
`runner` (a maradék stopja: keep|breakeven|trailing, None=default=trailing).

Fájlformátum (visszafelé kompatibilis): érték lehet sima string (csak preset) VAGY
dict {"preset","cautious","runner"}. Régi string → {"preset": string}.

A régi `risky_mode` (R gomb, kívülről is írható) MEGMARAD: `effective_preset`
egyesíti — ha itt 'off' de a risky_mode be van kapcsolva → 'risky'.
"""

import json
import threading
from pathlib import Path

from core import risky_mode
from core.risk_reduction import (
    PRESET_OFF, PRESET_RISKY, PRESET_HALVING, PRESET_SHIELD, PRESETS,
    RUNNER_KEEP, RUNNER_BREAKEVEN, RUNNER_TRAILING,
    default_config, wants_cautious_size,
)

PATH = Path(__file__).resolve().parents[1] / "data" / "risk_mode.json"

CYCLE = (PRESET_OFF, PRESET_RISKY, PRESET_HALVING, PRESET_SHIELD)
LABEL = {PRESET_OFF: "—", PRESET_RISKY: "R", PRESET_HALVING: "F", PRESET_SHIELD: "P"}
NAME  = {PRESET_OFF: "Ki", PRESET_RISKY: "Risky", PRESET_HALVING: "Felező",
         PRESET_SHIELD: "Pajzs"}
RUNNERS = (RUNNER_TRAILING, RUNNER_KEEP, RUNNER_BREAKEVEN)
RUNNER_NAME = {RUNNER_TRAILING: "Trailing", RUNNER_KEEP: "Marad távol",
               RUNNER_BREAKEVEN: "BE"}

_lock = threading.Lock()
_state: dict[str, dict] = {}


def _norm(v) -> dict:
    """Érték normalizálása dict-re (régi string → {preset})."""
    if isinstance(v, str):
        return {"preset": v if v in PRESETS else PRESET_OFF}
    if isinstance(v, dict):
        d = {"preset": v.get("preset", PRESET_OFF)}
        if d["preset"] not in PRESETS:
            d["preset"] = PRESET_OFF
        if v.get("cautious") is not None:
            d["cautious"] = bool(v["cautious"])
        if v.get("runner") in RUNNERS:
            d["runner"] = v["runner"]
        return d
    return {"preset": PRESET_OFF}


def load() -> dict:
    with _lock:
        try:
            if PATH.exists():
                with open(PATH, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    _state.clear()
                    _state.update({str(k): _norm(v) for k, v in data.items()})
        except Exception:
            pass
        return dict(_state)


def _entry(symbol: str) -> dict:
    return _state.get(symbol, {"preset": PRESET_OFF})


def get_preset(symbol: str) -> str:
    with _lock:
        return _entry(symbol).get("preset", PRESET_OFF)


def get_runner(symbol: str) -> str:
    with _lock:
        return _entry(symbol).get("runner", default_config()["runner_stop"])


def get_cautious(symbol: str):
    """None = a preset szerint (Risky→igen); True/False = kézi felülbírálás."""
    with _lock:
        return _entry(symbol).get("cautious", None)


def _set(symbol: str, **kw):
    with _lock:
        d = dict(_entry(symbol))
        d.update(kw)
        _state[symbol] = _norm(d)
        _save_locked()


def set_preset(symbol: str, preset: str):
    _set(symbol, preset=preset if preset in PRESETS else PRESET_OFF)


def set_runner(symbol: str, runner: str):
    _set(symbol, runner=runner if runner in RUNNERS else RUNNER_TRAILING)


def set_cautious(symbol: str, value):
    _set(symbol, cautious=(None if value is None else bool(value)))


def cycle_preset(symbol: str) -> str:
    with _lock:
        d = dict(_entry(symbol))
        cur = d.get("preset", PRESET_OFF)
        nxt = CYCLE[(CYCLE.index(cur) + 1) % len(CYCLE)] if cur in CYCLE else PRESET_RISKY
        d["preset"] = nxt
        _state[symbol] = _norm(d)
        _save_locked()
        return nxt


def effective_preset(symbol: str) -> str:
    """A ténylegesen érvényes preset: a per-pár választás, DE ha 'off' és a régi
    risky_mode (R gomb / külső fájl) be van kapcsolva → 'risky'."""
    p = get_preset(symbol)
    if p == PRESET_OFF and risky_mode.is_risky(symbol):
        return PRESET_RISKY
    return p


def spec_for(symbol: str) -> dict:
    """Teljes run_pair kockázatcsökkentő spec az adott párra (preset + runner +
    cautious felülbírálás). A run_pair a `cautious` kulcsot a méretezéshez nézi."""
    preset = effective_preset(symbol)
    spec = {**default_config(), "preset": preset, "runner_stop": get_runner(symbol)}
    c = get_cautious(symbol)
    spec["cautious"] = wants_cautious_size(preset) if c is None else bool(c)
    return spec


def all_states() -> dict:
    with _lock:
        return {k: dict(v) for k, v in _state.items()}


def _save_locked():
    try:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_state, f, indent=2, ensure_ascii=False)
        tmp.replace(PATH)
    except Exception:
        pass
