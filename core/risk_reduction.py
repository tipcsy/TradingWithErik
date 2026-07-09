"""
Kockázatcsökkentő technikák — presetek + LOT-LÉTRA feloldás (stratégia-független).

A megbeszélt 3 tengely (lásd Obsidian: Kockázatcsökkentés.md):
  1. Belépő méret            : normál | felezett (óvatos)      → wants_cautious_size
  2a. Részleges zárás 1R-nél : Ki | 50% (Felező) | 75% (Pajzs) → target_fraction
  2b. (maradék) stop         : marad TÁVOL | BE | Trailing      → runner_stop

Ez a modul TISZTA logika (nincs MT5/backtest/tkinter függés): adott preset + a
pozíció aktuális lotja → mit tegyünk (mennyit zárjunk le, mi a runner stopja),
a **lot-létrával degradálva**. A motor (backtest + live) ezt hívja 1R elérésekor.

KRITIKUS invariáns: a részleges zárás mindig **≥ 50%**, különben — ha az ár
visszafordul és kistoppol — a lezárt fél nyeresége NEM fedezi a maradék
veszteségét, azaz nettó MÍNUSZ lenne (pont a lényeg veszne el). Ha a pozíció
túl kicsi ahhoz, hogy ≥50%-ot lezárva a runner ≥ min_lot maradjon → nincs
részleges zárás, a hívó BE-húzásra (Risky) esik vissza.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# ── Presetek (2a tengely fő értékei + a Risky mint kombináció) ──────────────
PRESET_OFF     = "off"       # nincs kockázatcsökkentés
PRESET_RISKY   = "risky"     # óvatos méret + BE-húzás (NINCS részleges zárás)
PRESET_HALVING = "halving"   # Felező: 50% zár 1R-nél
PRESET_SHIELD  = "shield"    # Pajzs: 75% zár 1R-nél
PRESETS = (PRESET_OFF, PRESET_RISKY, PRESET_HALVING, PRESET_SHIELD)

# ── runner-stop (2b tengely) ────────────────────────────────────────────────
RUNNER_KEEP      = "keep"        # marad a TÁVOLI (eredeti) stop — a videó Pajzsa
RUNNER_BREAKEVEN = "breakeven"   # a maradék stopja a nyitóra (óvatosabb)
RUNNER_TRAILING  = "trailing"    # a maradék trailinggel fut

_EPS = 1e-9


def default_config() -> dict:
    """A kalibrációs paraméterek alapértékei (a stratégia-config felülírja)."""
    return {
        "trigger_R":        1.0,            # hány R-nél lép életbe a részleges zárás
        "halving_fraction": 0.5,            # Felező: a pozíció mekkora részét zárja
        "shield_fraction":  0.75,           # Pajzs: a nagyobb rész
        # A runner alapból TRAILINGgel fut (backteszt: alacsonyabb DD, azonos hozam,
        # mint a 'keep' távoli stop). A tiszta videó-Pajzs a 'keep' — haladó opció.
        "runner_stop":      RUNNER_TRAILING,
    }


def wants_cautious_size(preset: str) -> bool:
    """Óvatos (felezett) belépő-méret kell-e a preset alapján? (1. tengely.)
    A Risky felezi a méretet; a többi preset alap: normál. A GUI 'Óvatos méret'
    pipája ezt bármelyik presetnél felülbírálhatja — az a hívó (motor) dolga."""
    return preset == PRESET_RISKY


def target_fraction(preset: str, cfg: dict) -> float:
    """A preset által KÉRT részleges-zárás arány (a lot-létra ELŐTT). 0 = nincs."""
    if preset == PRESET_HALVING:
        return float(cfg.get("halving_fraction", 0.5))
    if preset == PRESET_SHIELD:
        return float(cfg.get("shield_fraction", 0.75))
    return 0.0   # off / risky → nincs részleges zárás


def closable_lot(cur_lot: float, fraction: float, min_lot: float,
                 lot_step: float) -> float:
    """A LOT-LÉTRA magja: a kért `fraction`-ből mennyit zárjunk le ténylegesen úgy,
    hogy (a) a lezárt rész **≥ 50%** (breakeven-biztos), (b) a runner ≥ min_lot,
    (c) minden lot_step-re illeszkedjen. 0.0 = nem osztható (pl. 1× min_lot)."""
    if fraction <= 0.0 or min_lot <= 0.0 or lot_step <= 0.0 or cur_lot <= 0.0:
        return 0.0
    # Legalább 2× min_lot kell, különben a runner min_lot alá menne.
    if cur_lot < 2.0 * min_lot - _EPS:
        return 0.0

    # Alsó korlát: legalább 50% (különben stopnál nettó mínusz) — step-re felfelé.
    lower = math.ceil(0.5 * cur_lot / lot_step - _EPS) * lot_step
    # Felső korlát: a runner ≥ min_lot maradjon.
    upper = cur_lot - min_lot
    # A preset céljához legközelebbi step-mennyiség.
    want = round(cur_lot * fraction / lot_step) * lot_step

    closed = min(max(want, lower), upper)
    # step-illesztés (lefelé, hogy sose lépjük túl az upper-t)
    closed = math.floor(closed / lot_step + _EPS) * lot_step
    if closed < min_lot - _EPS or closed < lower - _EPS:
        return 0.0
    return round(closed, 8)


@dataclass
class Plan:
    """Amit a technika 1R-nél tesz — a motor ezt hajtja végre."""
    close_lot: float     # ennyi lotot zárjon le (0 = nincs részleges zárás)
    runner_stop: str     # a maradékra: keep|breakeven|trailing
    effective: str       # a TÉNYLEGESEN alkalmazott technika (UI/log)


def plan_at_trigger(preset: str, cfg: dict, cur_lot: float, min_lot: float,
                    lot_step: float) -> Plan:
    """1R elérésekor: mit tegyünk a pozícióval, a lot-létrával DEGRADÁLVA.

    - off              → nincs teendő.
    - risky            → nincs részleges zárás, a runner (teljes) stopja BE-re.
    - halving/shield   → részleges zárás (ha a lot engedi), a runner stopja a
                         cfg['runner_stop'] szerint. Ha a lot túl kicsi az osztáshoz,
                         DEGRADÁL risky-re (BE-húzás, nincs zárás).
    A `effective` a UI/log számára a ténylegesen alkalmazott technikát adja."""
    if preset == PRESET_OFF:
        return Plan(0.0, RUNNER_KEEP, PRESET_OFF)
    if preset == PRESET_RISKY:
        return Plan(0.0, RUNNER_BREAKEVEN, PRESET_RISKY)

    frac = target_fraction(preset, cfg)
    closed = closable_lot(cur_lot, frac, min_lot, lot_step)
    if closed <= 0.0:
        # nem osztható (kis pozíció) → Risky/BE fallback
        return Plan(0.0, RUNNER_BREAKEVEN, PRESET_RISKY)

    runner = cfg.get("runner_stop", RUNNER_KEEP)
    # Tényleges technika: ha a Pajzs (75%) nem fért ki és inkább felező-szintű lett,
    # jelöljük halving-nak (a UI így pontosan mutatja, mi valósult meg).
    achieved = closed / cur_lot
    eff = preset
    if preset == PRESET_SHIELD and achieved < 0.66:
        eff = PRESET_HALVING
    return Plan(round(closed, 8), runner, eff)
