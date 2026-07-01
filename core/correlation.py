"""
Korreláció-/devizakitettség-védelem.

Minden pozíciót devizákra bont (EURUSD BUY = +EUR / −USD). Két instrumentum
akkor "korrelált", ha LEGALÁBB egy devizában AZONOS irányú kitettséget halmoz
(pl. EURUSD BUY és GBPUSD BUY is short-USD). Így nem kell korrelációs listát
karbantartani, és az indok jól megmagyarázható ("halmozott short-USD").

A K-mód GLOBÁLIS, 4 állapotú, és perzisztens (data/correlation_mode.json), hogy
a felület gombja a mérvadó, látható igazság legyen — nem egy elfeledett config-flag.
"""

import json
import threading
from pathlib import Path
from typing import Optional

PATH = Path(__file__).resolve().parents[1] / "data" / "correlation_mode.json"

# Állapotok (a K gomb körbe lépteti)
INACTIVE = "Inaktív"
ALERT    = "Jelző"          # csak jelöl/villog, nem avatkozik be
STRONGER = "Csak erősebb"   # korrelált újat blokkol (az erősebb nyit elsőként)
HALF     = "Fél méret"      # korrelált pozíció fele mérettel
MODES = [INACTIVE, ALERT, STRONGER, HALF]

_lock = threading.Lock()
_mode = ALERT   # alap: csak jelez, semmit nem tilt csendben


def load() -> str:
    global _mode
    with _lock:
        try:
            if PATH.exists():
                with open(PATH, encoding="utf-8") as f:
                    m = json.load(f).get("mode")
                if m in MODES:
                    _mode = m
        except Exception:
            pass
        return _mode


def get_mode() -> str:
    with _lock:
        return _mode


def set_mode(mode: str):
    global _mode
    if mode not in MODES:
        return
    with _lock:
        _mode = mode
        _save_locked()


def cycle() -> str:
    """A következő állapotra lép (gombnyomásra) és menti."""
    global _mode
    with _lock:
        _mode = MODES[(MODES.index(_mode) + 1) % len(MODES)]
        _save_locked()
        return _mode


def _save_locked():
    try:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"mode": _mode}, f, indent=2, ensure_ascii=False)
        tmp.replace(PATH)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Devizakitettség
# ---------------------------------------------------------------------------

def pair_currencies(symbol: str) -> Optional[tuple]:
    """(base, quote) ha a szimbólum 6 betűs devizapár (pl. EURUSD, XAUUSD),
    egyébként None (index/egyéb — ezekre nem alkalmazunk deviza-halmozást)."""
    s = symbol.upper()
    if len(s) == 6 and s.isalpha():
        return s[:3], s[3:]
    return None


def exposure(symbol: str, direction: str) -> dict:
    """{deviza: +1/−1} kitettség. BUY: +base/−quote, SELL: −base/+quote."""
    cc = pair_currencies(symbol)
    if not cc:
        return {}
    base, quote = cc
    if direction == "BUY":
        return {base: +1, quote: -1}
    if direction == "SELL":
        return {base: -1, quote: +1}
    return {}


def shared_exposure(symbol: str, direction: str, others: list) -> list:
    """A jelölttel AZONOS irányú deviza-kitettséget halmozó instrumentumok.

    others: [(symbol, direction), ...] (a saját szimbólumot ne tartalmazza).
    Visszaad: a halmozó szimbólumok listája (üres = nincs ütközés).
    """
    cand = exposure(symbol, direction)
    if not cand:
        return []
    out = []
    for osym, odir in others:
        if osym == symbol:
            continue
        oexp = exposure(osym, odir)
        for ccy, sign in cand.items():
            if ccy in oexp and (oexp[ccy] > 0) == (sign > 0):
                out.append(osym)
                break
    return out
