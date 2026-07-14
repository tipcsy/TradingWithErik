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
    RUNNER_KEEP, RUNNER_BREAKEVEN, RUNNER_TRAILING, RUNNER_EXIT,
    default_config, wants_cautious_size,
)
from core import exit_signal

PATH = Path(__file__).resolve().parents[1] / "data" / "risk_mode.json"

CYCLE = (PRESET_OFF, PRESET_RISKY, PRESET_HALVING, PRESET_SHIELD)
LABEL = {PRESET_OFF: "—", PRESET_RISKY: "R", PRESET_HALVING: "F", PRESET_SHIELD: "P"}
NAME  = {PRESET_OFF: "Ki", PRESET_RISKY: "Risky", PRESET_HALVING: "Felező",
         PRESET_SHIELD: "Pajzs"}
RUNNERS = (RUNNER_TRAILING, RUNNER_KEEP, RUNNER_BREAKEVEN, RUNNER_EXIT)
RUNNER_NAME = {RUNNER_TRAILING: "Trailing", RUNNER_KEEP: "Marad távol",
               RUNNER_BREAKEVEN: "BE", RUNNER_EXIT: "Kiszállási jel"}

# A per-pár kiszállási-modul beállítás mezői (a core.exit_signal.default_config
# kulcsai az `enabled` NÉLKÜL — az `enabled`-et a runner==exit vezérli).
_EXIT_KEYS = ("indicator", "timeframe", "st_period", "st_multiplier",
              "wpr_period", "wpr_ma_period", "osc", "div_period", "div_pivot")

_lock = threading.Lock()
_state: dict[str, dict] = {}

# Numerikus kalibrációs override-ok (az optimalizáló írhatja): ha jelen vannak, a
# spec_for felülírja velük a default_config() megfelelő kulcsait. Hiányukban a
# default_config érvényes (visszafelé kompatibilis: a régi fájlokban nincsenek).
_CALIB_KEYS = ("trigger_R", "halving_fraction", "shield_fraction")


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
        for k in _CALIB_KEYS:
            if isinstance(v.get(k), (int, float)):
                d[k] = float(v[k])
        # Per-pár kiszállási-modul beállítás (csak az ismert kulcsok, ha van).
        ex = v.get("exit")
        if isinstance(ex, dict):
            d["exit"] = {k: ex[k] for k in _EXIT_KEYS if k in ex}
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


def get_calibration(symbol: str) -> dict:
    """A per-pár numerikus kalibrációs override-ok (trigger_R/frakciók), ha vannak.
    Üres dict, ha nincs (→ a default_config érvényes)."""
    with _lock:
        e = _entry(symbol)
        return {k: float(e[k]) for k in _CALIB_KEYS if isinstance(e.get(k), (int, float))}


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


def get_exit_config(symbol: str) -> dict:
    """A per-pár KISZÁLLÁSI-MODUL beállítása (core.exit_signal cfg formátumban).
    A default_config-ra ráolvasztjuk a per-pár override-ot, és az `enabled`-et a
    runner-mód dönti: csak akkor él, ha a runner == 'exit' (Kiszállási jel)."""
    cfg = exit_signal.default_config()
    with _lock:
        ov = _entry(symbol).get("exit")
        runner = _entry(symbol).get("runner", default_config()["runner_stop"])
    if isinstance(ov, dict):
        cfg.update({k: ov[k] for k in _EXIT_KEYS if k in ov})
    cfg["enabled"] = (runner == RUNNER_EXIT)
    return cfg


def set_exit_config(symbol: str, **params):
    """A per-pár kiszállási-modul mezőinek frissítése (indicator/paraméterek).
    Csak az ismert kulcsokat tartja meg; a meglévő override-ot kiegészíti."""
    upd = {k: params[k] for k in _EXIT_KEYS if k in params}
    if not upd:
        return
    with _lock:
        d = dict(_entry(symbol))
        ex = dict(d.get("exit") or {})
        ex.update(upd)
        d["exit"] = ex
        _state[symbol] = _norm(d)
        _save_locked()


def set_cautious(symbol: str, value):
    _set(symbol, cautious=(None if value is None else bool(value)))


def set_from_optimizer(symbol: str, rr: dict):
    """Az optimalizáló nyertes rr-jét a per-pár állapotba írja: preset + runner +
    cautious + numerikus kalibráció (trigger_R/frakciók). A live/GUI ezt veszi át."""
    if not rr:
        return
    kw = {"preset": rr.get("preset", PRESET_OFF),
          "runner": rr.get("runner_stop", RUNNER_TRAILING),
          "cautious": rr.get("cautious")}
    for k in _CALIB_KEYS:
        if isinstance(rr.get(k), (int, float)):
            kw[k] = float(rr[k])
    _set(symbol, **kw)


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
    # Numerikus kalibráció-override (ha az optimalizáló beírta) — különben a default.
    spec.update(get_calibration(symbol))
    c = get_cautious(symbol)
    spec["cautious"] = wants_cautious_size(preset) if c is None else bool(c)
    # A kiszállási-modul beállítása (enabled = runner==exit) — a motor/backtest ezt
    # adja tovább az exit_signal.exit_triggered-nek a runner zárásához.
    spec["exit"] = get_exit_config(symbol)
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
