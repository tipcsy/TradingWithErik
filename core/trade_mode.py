"""
Kereskedési mód per pár+stratégia: VALÓDI kötés vagy CSAK JELZÉS.

    "pairs": { "EURUSD": {
        "strategy_mode": {"wpr_sma": "signal", "ml_ai": "live"}
    } }

`live`   — a megszokott viselkedés: a motor ténylegesen megnyitja a pozíciót.
`signal` — a motor MINDENT ugyanúgy kiszámol (jel, kapuk, SL/TP, lot), de NEM
           küld megbízást: csak riasztást ad a charton és naplóz. Teszteléshez:
           így egy új stratégia/paraméterkészlet élesben figyelhető pénz nélkül.

Alapérték: `live`. Egy régi config.json tehát változatlanul kereskedik — a
„csak jelzés" mindig KIFEJEZETT választás, sosem véletlen mellékhatás.

SZÁNDÉKOSAN külön modul a `core.viz_prefs`-től: az ott lévő kapcsolók
MEGJELENÍTÉST vezérelnek, ez viszont azt dönti el, megy-e ki valódi megbízás.
Egy helyen, könnyen auditálhatóan.
"""

from __future__ import annotations

MODE_LIVE   = "live"      # valódi kötés (alapértelmezett)
MODE_SIGNAL = "signal"    # csak jelzés/riasztás, megbízás NEM megy ki

# Emberi nevek a felülethez (a táblázat legördülője ezeket mutatja).
LABELS = {MODE_LIVE: "Valódi", MODE_SIGNAL: "Jelzés"}


def mode_of(cfg: dict, symbol: str, strategy_name: str) -> str:
    """Az adott pár+stratégia kereskedési módja. Ismeretlen/hiányzó érték →
    `live` (biztonságos alapértelmezés a VISSZAFELÉ kompatibilitáshoz: ami eddig
    kereskedett, az ezután is kereskedik)."""
    pc = (cfg.get("pairs") or {}).get(symbol)
    if not isinstance(pc, dict):
        return MODE_LIVE
    per = pc.get("strategy_mode")
    if not isinstance(per, dict):
        return MODE_LIVE
    return MODE_SIGNAL if per.get(strategy_name) == MODE_SIGNAL else MODE_LIVE


def is_signal_only(cfg: dict, symbol: str, strategy_name: str) -> bool:
    """True, ha ezen a páron ez a stratégia CSAK JELEZ (nem köt)."""
    return mode_of(cfg, symbol, strategy_name) == MODE_SIGNAL


def set_mode(cfg: dict, symbol: str, strategy_name: str, mode: str) -> None:
    """Mód beállítása (a hívó menti a configot). `live` esetén a kulcsot TÖRLI,
    hogy az alapértelmezés ne szennyezze a configot — és hogy a fájlban a
    `strategy_mode` jelenléte mindig valódi eltérést jelentsen."""
    pc  = cfg.setdefault("pairs", {}).setdefault(symbol, {})
    per = pc.get("strategy_mode")
    if not isinstance(per, dict):
        per = {}
    if mode == MODE_SIGNAL:
        per[strategy_name] = MODE_SIGNAL
    else:
        per.pop(strategy_name, None)
    if per:
        pc["strategy_mode"] = per
    else:
        pc.pop("strategy_mode", None)


def signal_only_pairs(cfg: dict) -> list:
    """(symbol, strategy) párok, ahol CSAK JELZÉS van. A dashboard indításkor
    kiírja őket, hogy sose maradjon ÉSZREVÉTLENÜL egy nem-kereskedő stratégia."""
    out = []
    for sym, pc in (cfg.get("pairs") or {}).items():
        if not isinstance(pc, dict):
            continue
        per = pc.get("strategy_mode")
        if isinstance(per, dict):
            out += [(sym, n) for n, m in per.items() if m == MODE_SIGNAL]
    return sorted(out)
