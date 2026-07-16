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
PRESET_FIBO    = "fibo"      # Fibo: stop-húzás a belépő→TP táv 61,8%-ánál (nincs zárás)
PRESET_THIRDS  = "thirds"    # Harmados (1/3–2/3): R-alapú stop-létra (nincs zárás)
# Pajzs↔Fibo auto (tananyag 3. pont): alaphelyzetben PAJZS; NAGY mozgásnál
# (ATR >> átlag a belépéskor) FIBO — hagyjuk futni, később húzunk stopot.
PRESET_SHIELD_FIBO = "shield_fibo"
PRESETS = (PRESET_OFF, PRESET_RISKY, PRESET_HALVING, PRESET_SHIELD, PRESET_FIBO,
           PRESET_THIRDS, PRESET_SHIELD_FIBO)

# ── runner-stop (2b tengely) ────────────────────────────────────────────────
RUNNER_KEEP      = "keep"        # marad a TÁVOLI (eredeti) stop — a videó Pajzsa
RUNNER_BREAKEVEN = "breakeven"   # a maradék stopja a nyitóra (óvatosabb)
RUNNER_TRAILING  = "trailing"    # a maradék trailinggel fut
RUNNER_EXIT      = "exit"        # a maradékot KISZÁLLÁSI JELRE zárjuk (core.exit_signal)

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
        # Fibo preset: a belépő→TP távra húzott Fibonacci. A trigger a fibo_level
        # pontján (tananyag: 61,8%); a stop a fibo_stop_level szintre áll
        # (0.0 = breakeven; variánsok: 0.236 / 0.382 — bezárt rész-profit).
        "fibo_level":       0.618,
        "fibo_stop_level":  0.0,
        # Harmados (1/3–2/3, „Birger") preset: az alap-táv R-ben. A tananyag
        # 1,5R-rel számol (távoli, ~3R célárnál); nálunk a TP tipikusan 1,5R-nél
        # ül (tp_rr_ratio), ezért az alap 1,0R — így a lépcső a TP ELŐTT elsül.
        # 1. lépcső: az ár megteszi az alap-távot → stop az alap 1/3-ára (profitban).
        # 2. lépcső: az ár a célárnál → stop a 2/3-ra (hard TP-nél ritkán él —
        # akkor számít, ha a TP-t kézzel kivetted/kitoltad).
        "thirds_base_R":    1.0,
        # Pajzs↔Fibo auto: e szorzó FÖLÖTT számít „nagy mozgásnak" a piac
        # (belépéskori ATR > big_move_atr_mult × ATR-átlag) → Fibo; alatta Pajzs.
        # 2.0-val a nagy mozgás RITKA volt (az atr_max_pct vol-szűrő is vágja a
        # kaotikus belépőket) → 1.5, hogy a Fibo-ág érdemben szerephez jusson.
        "big_move_atr_mult": 1.5,
        # Cost-cut (tananyag 2.6): IDŐ-STOP, bármely presettel kombinálható.
        # Ha a nyitás után cost_cut_bars fő-timeframe gyertyával a pozíció még
        # VESZTESÉGES → piaci áron zárjuk (a kanóc/zaj korai levágása töredék-R
        # veszteséggel, a teljes SL kivárása helyett). False = kikapcsolva.
        "cost_cut":         False,
        "cost_cut_bars":    12,
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


def fibo_levels(open_price: float, tp_price: float, cfg: dict) -> tuple[float, float]:
    """Fibo preset: (trigger_ár, új_stop_ár) a belépő→TP távra húzott Fibonacci
    szerint (tananyag 2.2: NEM a hullámra, hanem a beszálló→célár távra).

    A táv ELŐJELES (BUY: +, SELL: −) → mindkét irányra helyes képlet:
      trigger  = open + táv × fibo_level      (alap 0.618)
      új stop  = open + táv × fibo_stop_level (0.0 = breakeven; 0.236/0.382 =
                 rész-profit bezárva, nem túl közel az árhoz → a zaj nem ver ki)
    (0.0, 0.0), ha nincs érvényes TP (a Fibo TP-távra épül — enélkül nem értelmezhető)."""
    if not tp_price or not open_price:
        return (0.0, 0.0)
    dist = tp_price - open_price
    if dist == 0.0:
        return (0.0, 0.0)
    lvl  = float(cfg.get("fibo_level", 0.618))
    slvl = float(cfg.get("fibo_stop_level", 0.0))
    return (open_price + dist * lvl, open_price + dist * slvl)


def big_move(atr_now: float, atr_avg: float, cfg: dict) -> bool:
    """Pajzs↔Fibo auto: „nagy mozgás"-e a piac a belépéskor? (ATR a szokásos
    átlag big_move_atr_mult-szorosa fölött.) Érvénytelen inputnál False (→ Pajzs,
    a konzervatív alaphelyzet)."""
    if not atr_now or not atr_avg or atr_avg <= 0:
        return False
    return atr_now > float(cfg.get("big_move_atr_mult", 2.0)) * atr_avg


def thirds_levels(open_price: float, risk_dist: float, is_buy: bool,
                  cfg: dict) -> tuple[float, float, float]:
    """Harmados (1/3–2/3, „Birger"): (trigger_ár, stop1_ár, stop2_ár) R-alapon.

    alap-táv = thirds_base_R × R (a kezdeti kockázat-távolság `risk_dist`).
      trigger : az ár megtette az alap-távot   → stop1 = alap-táv 1/3-a (profitban)
      célárnál: stop2 = alap-táv 2/3-a (a hívó a saját TP-érintésén ellenőrzi)
    (0,0,0), ha nincs érvényes kockázat-táv."""
    if risk_dist <= 0.0:
        return (0.0, 0.0, 0.0)
    base = float(cfg.get("thirds_base_R", 1.0)) * risk_dist
    sgn  = 1.0 if is_buy else -1.0
    return (open_price + sgn * base,
            open_price + sgn * base / 3.0,
            open_price + sgn * base * 2.0 / 3.0)


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
    if preset == PRESET_FIBO:
        # A Fibo nem 1R-alapú és nem zár részlegesen — a motor a fibo_levels()
        # szerinti stop-húzással kezeli (ez az ág csak védelem, ha mégis idehívnák).
        return Plan(0.0, RUNNER_KEEP, PRESET_FIBO)
    if preset == PRESET_THIRDS:
        # A Harmados sem zár részlegesen — a motor a thirds_levels() szerinti
        # stop-létrával kezeli (ez az ág csak védelem).
        return Plan(0.0, RUNNER_KEEP, PRESET_THIRDS)
    if preset == PRESET_SHIELD_FIBO:
        # A Pajzs↔Fibo autót a motor belépéskor HATÁSOS presetre oldja fel
        # (shield vagy fibo) — ide már nem juthat el; védelemként Pajzsként kezeljük.
        preset = PRESET_SHIELD

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
