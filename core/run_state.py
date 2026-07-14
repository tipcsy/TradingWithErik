"""Kereskedés-SZÁNDÉK per (instrumentum × stratégia) — restart-biztos állapot.

A felhasználó szándéka (Play/Stop) a `config.json`-ba perzisztál, hogy újraindítás
után a `live_trader` reconciler visszaállíthassa (a kereskedés magától folytatódik
azoknál, amelyeknél futott). A TÁROLÁS a `pairs.<sym>` alatt:

    "pairs": { "EURUSD": { ..., "run_state": { "wpr_sma": "live" } } }

Értékek: ``"live"`` | ``"stopped"`` (a trading szándéka). Az OPTIMALIZÁLÁS NEM ide
kerül — az a fájlrendszerből derül ki (`params_store.unfinished_studies`), így egy
megszakadt study markerből mindig folytatható, és nem kell külön perzisztálni.

Visszafelé kompatibilitás: ha egy párnál még NINCS `run_state`, a régi
szimbólum-szintű `enabled` (bool) dönt — de CSAK az ELSŐDLEGES stratégiára (a
többi ilyenkor "stopped"). Így a régi config.json változatlanul működik, és amint
egyszer Play/Stop-ot nyomsz, onnantól a per-stratégia map az igazság.

Az `enabled` flaget SZINKRONBAN tartjuk (= van-e BÁRMELY stratégia "live"), hogy a
`run()` szimbólum-szintű kapuja és minden régi olvasó (viz, quality) változatlan
maradjon.
"""

from __future__ import annotations

LIVE = "live"
STOPPED = "stopped"


def _pair(cfg: dict, symbol: str) -> dict | None:
    p = (cfg.get("pairs") or {}).get(symbol)
    return p if isinstance(p, dict) else None


def get_state(cfg: dict, symbol: str, strategy: str, primary: str | None = None) -> str:
    """A (symbol, strategy) kereskedés-szándéka: ``"live"`` vagy ``"stopped"``.

    Feloldás: (1) ha van `run_state[strategy]`, az dönt; (2) különben legacy —
    a szimbólum-szintű `enabled` CSAK az elsődleges stratégiára ad "live"-ot, a
    többi "stopped". `primary=None` → nincs legacy-live (minden nem-jelölt stopped)."""
    pc = _pair(cfg, symbol)
    if pc is None:
        return STOPPED
    rs = pc.get("run_state")
    if isinstance(rs, dict) and strategy in rs:
        return LIVE if rs.get(strategy) == LIVE else STOPPED
    # legacy: enabled → csak az elsődleges stratégia
    if primary is not None and strategy == primary and pc.get("enabled", False):
        return LIVE
    return STOPPED


def live_strategies(cfg: dict, symbol: str, strat_names: list[str],
                    primary: str | None = None) -> list[str]:
    """A megadott stratégiák közül azok, amelyeknek a szándéka "live".

    Legacy (nincs `run_state` map): a szimbólum-szintű `enabled` az ÖSSZES megadott
    (engedélyezett) stratégiára "live" — pontosan a jelenlegi `run()` viselkedés,
    hogy a régi config.json bitre ugyanúgy induljon. Amint egyszer Play/Stop-ot
    nyomsz, a map lesz az igazság (stratégiánként)."""
    pc = _pair(cfg, symbol)
    if pc is None:
        return []
    rs = pc.get("run_state")
    if isinstance(rs, dict):
        return [s for s in strat_names if rs.get(s) == LIVE]
    return list(strat_names) if pc.get("enabled", False) else []


def set_state(cfg: dict, symbol: str, strategy: str, state: str) -> None:
    """A (symbol, strategy) szándék beállítása a futásidejű cfg-ben (helyben).

    Frissíti a `pairs.<sym>.run_state[strategy]`-t ÉS szinkronizálja a szimbólum-
    szintű `enabled`-et (= van-e bármely "live"). A KIÍRÁST (config.json mentés) a
    hívó (GUI `_save_main_config`) végzi — ez a modul csak a dict-et módosítja."""
    pc = _pair(cfg, symbol)
    if pc is None:
        return
    rs = pc.get("run_state")
    if not isinstance(rs, dict):
        rs = {}
        pc["run_state"] = rs
    rs[strategy] = LIVE if state == LIVE else STOPPED
    pc["enabled"] = any(v == LIVE for v in rs.values())
