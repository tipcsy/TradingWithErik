"""Kézzel nyitott pozíciók STRATÉGIÁHOZ RENDELÉSE (örökbefogadás) — perzisztens
JSON (`data/adopted_positions.json`).

MIÉRT KELL: a bot a saját pozícióit a MT5 **magic** száma alapján ismeri fel
(`live_trader.get_open_positions`). A magic (és a comment) az order elküldésekor
dől el, és **utólag NEM módosítható** — a `TRADE_ACTION_SLTP` csak az SL/TP-t
írja át, magicet/commentet nem lehet ráosztani egy már nyitott pozícióra. Ezért
a hozzárendelést A MI OLDALUNKON tartjuk nyilván: ticket → stratégia. A motor
onnantól úgy kezeli a pozíciót, mintha ő nyitotta volna (breakeven, trailing,
kockázatcsökkentés, kiszállási jel, cost-cut, pozícióépítés).

Amit az örökbefogadás NEM tud megváltoztatni: a bróker oldalán a pozíció magicje
és commentje marad a régi (kézi). Ha a program adatai elvesznének, a bróker
nem tudja, melyik stratégiához tartozott — ezért ez a fájl a nyilvántartás.

A lezárt pozíció bejegyzését NEM töröljük azonnal (`mark_closed`), hogy a
„Lezárt ma" fül még helyesen mutassa a stratégiát; a régieket a `prune` takarítja.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / "data" / "adopted_positions.json"

_lock = threading.Lock()
_state: dict[str, dict] = {}
_loaded = False


def _norm(v) -> dict | None:
    if not isinstance(v, dict):
        return None
    strat, sym = v.get("strategy"), v.get("symbol")
    if not strat or not sym:
        return None
    d = {"strategy": str(strat), "symbol": str(sym),
         "adopted_at": str(v.get("adopted_at") or "")}
    if v.get("closed_at"):
        d["closed_at"] = str(v["closed_at"])
    return d


def load() -> dict:
    """Beolvasás lemezről (idempotens). A modul a többi állapot-modullal azonos
    mintát követi: egy folyamaton belül a memóriabeli állapot az igazság."""
    global _loaded
    with _lock:
        try:
            if PATH.exists():
                with open(PATH, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    _state.clear()
                    for k, v in data.items():
                        n = _norm(v)
                        if n is not None:
                            _state[str(k)] = n
        except Exception:
            pass
        _loaded = True
        return dict(_state)


def _ensure_loaded():
    if not _loaded:
        load()


def _save_locked():
    try:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_state, f, indent=2, ensure_ascii=False)
        tmp.replace(PATH)
    except Exception:
        pass


def adopt(ticket: int, strategy: str, symbol: str):
    """A ticket ehhez a stratégiához tartozik — a motor sajátjaként kezeli."""
    _ensure_loaded()
    with _lock:
        _state[str(int(ticket))] = {
            "strategy": str(strategy), "symbol": str(symbol),
            "adopted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        _save_locked()


def release(ticket: int):
    """A hozzárendelés VISSZAVONÁSA (a felületről) — a motor elengedi a pozíciót
    (nem húzza tovább a stopot, nem zárja): újra tisztán kézi pozíció lesz."""
    _ensure_loaded()
    with _lock:
        if _state.pop(str(int(ticket)), None) is not None:
            _save_locked()


def mark_closed(ticket: int):
    """A pozíció lezárult — a bejegyzés MEGMARAD (a napló/„Lezárt ma" fül még
    hivatkozik rá), csak időbélyeget kap. A `prune` takarítja el később."""
    _ensure_loaded()
    with _lock:
        e = _state.get(str(int(ticket)))
        if e is not None and not e.get("closed_at"):
            e["closed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            _save_locked()


def strategy_of(ticket) -> str | None:
    """A tickethez rendelt stratégia neve (lezárt bejegyzésre is), vagy None."""
    _ensure_loaded()
    if ticket is None:
        return None
    with _lock:
        e = _state.get(str(int(ticket)))
        return e["strategy"] if e else None


def is_open_adopted(ticket) -> bool:
    """Örökbefogadott ÉS még nem lezárt-e ez a ticket."""
    _ensure_loaded()
    if ticket is None:
        return False
    with _lock:
        e = _state.get(str(int(ticket)))
        return bool(e) and not e.get("closed_at")


def tickets_for(strategy: str, symbol: str | None = None) -> set:
    """Az adott stratégiához (és opcionálisan szimbólumhoz) rendelt ÉLŐ ticketek."""
    _ensure_loaded()
    with _lock:
        return {int(k) for k, e in _state.items()
                if e["strategy"] == strategy and not e.get("closed_at")
                and (symbol is None or e["symbol"] == symbol)}


def pairs() -> set:
    """(symbol, strategy) párok, amelyeknek van ÉLŐ örökbefogadott pozíciójuk —
    a live loop ezekre gondoskodik a feldolgozásról."""
    _ensure_loaded()
    with _lock:
        return {(e["symbol"], e["strategy"]) for e in _state.values()
                if not e.get("closed_at")}


def prune(open_tickets=None, keep_days: int = 3):
    """Takarítás induláskor: a régen lezárt bejegyzések törlése. Ha `open_tickets`
    meg van adva, a MÁR NEM NYITOTT (de lezártként sem jelölt — pl. a program
    állása közben zárt) ticketek is lezártnak számítanak."""
    _ensure_loaded()
    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
    with _lock:
        changed = False
        for k in list(_state):
            e = _state[k]
            if open_tickets is not None and int(k) not in open_tickets \
                    and not e.get("closed_at"):
                e["closed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                changed = True
            ca = e.get("closed_at")
            if ca:
                try:
                    if datetime.fromisoformat(ca) < cutoff:
                        del _state[k]
                        changed = True
                except Exception:
                    pass
        if changed:
            _save_locked()
