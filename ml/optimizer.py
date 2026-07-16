"""
AI Paraméter Optimalizálás — Random / Grid Search

Működés:
  1. TRAIN adat (history_start → test_start_date): próbálja az összes kombinációt
  2. Legjobb kombináció kiválasztása (max. total_pnl, min. max_drawdown figyelembe véve)
  3. TEST adat (test_start_date → ma): out-of-sample validálás
  4. Eredmények mentése: data/optimized_params.json

Futtatás: python ml/optimizer.py
"""

import json
import logging
import math
import random
import sys
import time
from copy import deepcopy
from datetime import datetime
from itertools import product
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

try:
    import optuna
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    _OPTUNA_AVAILABLE = True
except ImportError:
    _OPTUNA_AVAILABLE = False

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.indicator_engine import compute_indicators
from trading.backtest import load_data, run_pair

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# Stratégia-hatókörű path-helperek a KÖZÖS, könnyű modulból (core.params_store) —
# innen re-exportálva, hogy a régi `from ml.optimizer import PARAMS_DIR/params_file`
# importok változatlanul működjenek. A tárolás elrendezését lásd ott.
from core.params_store import (            # noqa: E402  (re-export)
    PARAMS_DIR, set_active_strategy, active_strategy, strategy_dir,
    params_file, trials_file, study_db, done_marker, stop_marker, migrate_flat_layout,
)


# A trials CSV formátuma: ';' elválasztó + ',' tizedesjel (magyar Excel),
# utf-8-sig BOM. A GUI Excelben nyitja, ill. a paraméter-szerkesztő a `rank`
# oszlop (minőségi rangsor, 1 = legjobb) szerint tölti be az egyes sorokat.
def _write_trials_csv(rows: list[dict], out_csv: Path) -> int:
    if not rows:
        return 0
    df = pd.DataFrame(rows).sort_values("score", ascending=False).reset_index(drop=True)
    # Explicit sorszám (rank): a score-rendezés utáni pozíció → 1 = legjobb.
    df.insert(0, "rank", range(1, len(df) + 1))
    df.to_csv(out_csv, index=False, encoding="utf-8-sig", sep=";", decimal=",")
    return len(rows)


# ---------------------------------------------------------------------------
# Paraméter tér generálás
# ---------------------------------------------------------------------------

def _range(spec: dict) -> list:
    """Egész vagy float tartomány generálása a config alapján."""
    lo, hi, step = spec["min"], spec["max"], spec["step"]
    values = []
    v = lo
    while v <= hi + 1e-9:
        values.append(round(v, 6))
        v += step
    return values


def generate_random_params(opt_cfg: dict, base_params: dict, n: int,
                           constraints=None) -> list[dict]:
    """N db véletlen paraméter kombinációt generál.

    constraints: opcionális fn(params)->bool — a stratégia érvényesség-ellenőrzője
    (pl. WPR szint-sorrend). None → nincs szűrés.
    """
    ranges = {
        k: _range(v)
        for k, v in opt_cfg.items()
        if isinstance(v, dict) and "min" in v
    }

    combos = []
    seen = set()
    attempts = 0
    max_attempts = n * 20

    while len(combos) < n and attempts < max_attempts:
        attempts += 1
        p = deepcopy(base_params)
        for key, values in ranges.items():
            p[key] = random.choice(values)

        if constraints is not None and not constraints(p):
            continue

        key_tuple = tuple(sorted(p.items()))
        if key_tuple in seen:
            continue
        seen.add(key_tuple)
        combos.append(p)

    return combos


def generate_grid_params(opt_cfg: dict, base_params: dict,
                         constraints=None) -> list[dict]:
    """Teljes grid — csak kis paramétertérnél használandó!

    constraints: opcionális fn(params)->bool — a stratégia érvényesség-ellenőrzője.
    """
    ranges = {}
    fixed = deepcopy(base_params)

    for k, v in opt_cfg.items():
        if isinstance(v, dict) and "min" in v:
            ranges[k] = _range(v)
        # string értékek (pl. method) kihagyva

    keys = list(ranges.keys())
    combos = []
    for values in product(*[ranges[k] for k in keys]):
        p = deepcopy(fixed)
        for k, v in zip(keys, values):
            p[k] = v

        if constraints is not None and not constraints(p):
            continue

        combos.append(p)

    return combos


# ---------------------------------------------------------------------------
# Értékelési metrika
# ---------------------------------------------------------------------------

def score(summary: dict, min_trades: int = 10) -> float:
    """
    Egyetlen szám ami maximalizálandó.
    Kevés trade esetén bünteti (nem megbízható).
    Drawdown büntető szorzó.
    """
    trades = summary.get("trades", 0)
    if trades < min_trades:
        return -999999.0

    pnl      = summary.get("total_pnl", 0.0)
    max_dd   = summary.get("max_drawdown", 1.0)
    win_rate = summary.get("win_rate", 0.0)
    pf       = summary.get("profit_factor", 1.0)

    if pnl <= 0:
        return pnl  # negatív → egyértelműen rossz

    # Drawdown büntető: 20% felett erősen büntet
    dd_penalty = 1.0 - max(0, max_dd - 0.20) * 3.0
    dd_penalty = max(0.1, dd_penalty)

    return pnl * dd_penalty * math.sqrt(win_rate) * min(pf, 5.0)


# ---------------------------------------------------------------------------
# Walk-forward ablakok generálása
# ---------------------------------------------------------------------------

def _walk_forward_windows(df_m15: pd.DataFrame, n_splits: int = 4,
                           train_months: int = 6, test_months: int = 2) -> list[dict]:
    """
    Gördülő ablakos validáció időablakai.
    Minden ablakban: train_months tanítás + test_months validálás.
    """
    last_ts  = df_m15.index[-1]
    first_ts = df_m15.index[0]
    windows  = []
    test_end = last_ts

    for _ in range(n_splits):
        test_start  = test_end  - pd.DateOffset(months=test_months)
        train_start = test_start - pd.DateOffset(months=train_months)

        if train_start < first_ts:
            break

        # UTC-aware ha szükséges
        def _tz(ts):
            return ts.tz_localize("UTC") if ts.tzinfo is None and df_m15.index.tzinfo is not None else ts

        windows.append({
            "train_start": _tz(train_start),
            "test_start":  _tz(test_start),
            "test_end":    _tz(test_end),
        })
        test_end = test_start

    return list(reversed(windows))  # kronológiai sorrendben


def _score_trades(trades: list, initial_balance: float, min_trades: int = 5) -> float:
    """Zárt trade lista → egyetlen score szám."""
    if len(trades) < min_trades:
        return -999999.0

    pnl_list = [t.pnl_usd for t in trades]
    wins     = [p for p in pnl_list if p > 0]
    losses   = [p for p in pnl_list if p <= 0]

    if not wins:
        return sum(pnl_list)

    balance = initial_balance
    peak    = balance
    max_dd  = 0.0
    for p in pnl_list:
        balance += p
        peak     = max(peak, balance)
        dd       = (peak - balance) / peak if peak > 0 else 0
        max_dd   = max(max_dd, dd)

    total_pnl  = sum(pnl_list)
    win_rate   = len(wins) / len(trades)
    pf         = abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else 5.0

    if total_pnl <= 0:
        return total_pnl

    dd_penalty = max(0.1, 1.0 - max(0, max_dd - 0.20) * 3.0)
    return total_pnl * dd_penalty * math.sqrt(win_rate) * min(pf, 5.0)


# ---------------------------------------------------------------------------
# Optuna alapú optimalizálás walk-forward validációval
# ---------------------------------------------------------------------------

# ── Kockázatcsökkentés (rr) optimalizálási tere — opt-in: optimizer.optimize_rr ──
# Framework-szintű (bármely stratégiával), ezért a config.json optimizer-blokkban
# felülírható (optimizer.rr_space); a preset+runner kategorikus, a trigger_R és a
# frakciók float dimenziók. KRITIKUS invariáns: a részleges zárás ≥50% → a
# halving_fraction alsó határa ≥0.5 (különben stopnál nettó mínusz).
_RR_PRESETS = ("off", "risky", "halving", "shield")
_RR_RUNNERS = ("trailing", "keep", "breakeven")
_RR_SPACE_DEFAULT = {
    "trigger_R":        {"min": 0.5, "max": 2.0, "step": 0.1},
    "halving_fraction": {"min": 0.5, "max": 0.75, "step": 0.05},
    "shield_fraction":  {"min": 0.6, "max": 0.9, "step": 0.05},
}


def _suggest_rr(trial, opt_cfg: dict) -> dict:
    """Optuna trial → TELJES rr-spec (preset + runner + trigger_R/frakció kalibráció).

    Mindig FIX keresési teret ad (a preset akkor is off lehet) — a preset='off'
    esetén a run_pair felé None-ra fordítjuk (`_rr_for_run`), de a spec-et
    rögzítjük (a CSV-be + a nyertes rr-hez). A `cautious` a preset szerint."""
    from core import risk_reduction as _rr
    space = {**_RR_SPACE_DEFAULT, **(opt_cfg.get("rr_space") or {})}
    preset = trial.suggest_categorical("rr_preset", list(_RR_PRESETS))
    runner = trial.suggest_categorical("rr_runner", list(_RR_RUNNERS))
    vals = {}
    for key in ("trigger_R", "halving_fraction", "shield_fraction"):
        s = space[key]
        vals[key] = trial.suggest_float(f"rr_{key}", float(s["min"]),
                                        float(s["max"]), step=float(s["step"]))
    return {"preset": preset, "runner_stop": runner,
            "cautious": _rr.wants_cautious_size(preset), **vals}


def _rr_for_run(spec: "dict | None"):
    """A run_pair-nek átadható rr: None ha nincs spec vagy a preset 'off' (tiszta
    OFF-viselkedés, bitazonos a rr=None úttal)."""
    if not spec or spec.get("preset", "off") == "off":
        return None
    return spec


def _dep_order(specs: dict) -> list:
    """A range-paraméterek suggeszt-SORRENDJE: a `gt`/`lt`-vel hivatkozott
    paramétereket ELŐBB kell suggesztálni, hogy a dinamikus tartomány (lásd
    `_suggest_params`) az ő értékükből szűkíthető legyen. Kahn-szerű topologikus
    rendezés; körkörös hivatkozásnál a maradékot az eredeti sorrendben fűzi hozzá
    (a constraints-szűrő úgyis elkapja az esetleges érvénytelent)."""
    deps = {}
    for k, s in specs.items():
        deps[k] = {r for r in (s.get("gt"), s.get("lt")) if r in specs}
    order, placed, remaining = [], set(), dict(deps)
    while remaining:
        ready = [k for k, refs in remaining.items() if refs <= placed]
        if not ready:                       # körkörös dep → ne akadjunk el
            order.extend(remaining.keys())
            break
        for k in ready:
            order.append(k); placed.add(k); del remaining[k]
    return order


def _suggest_params(trial, opt_cfg: dict, base_params: dict) -> dict:
    """Optuna trial → paraméter dict.

    A `gt`/`lt` metaadattal ellátott range-eket DINAMIKUSAN szűkíti a MÁR
    suggeszált paraméterek alapján → érvénytelen kombináció ELŐ SEM ÁLL (nincs
    elpazarolt trial): `gt: X` → szigorúan X fölött (X+step), `lt: Y` → szigorúan
    Y alatt (Y−step). A range-eket a `_dep_order` szerint suggeszti (a hivatkozott
    paraméterek előbb)."""
    params = deepcopy(base_params)
    specs = {k: v for k, v in opt_cfg.items()
             if isinstance(v, dict) and "min" in v}
    for key in _dep_order(specs):
        spec = specs[key]
        lo, hi, step = spec["min"], spec["max"], spec["step"]
        gt, lt = spec.get("gt"), spec.get("lt")
        if gt is not None and gt in params:
            lo = max(lo, params[gt] + step)          # szigorúan nagyobb
        if lt is not None and lt in params:
            hi = min(hi, params[lt] - step)          # szigorúan kisebb
        if lo > hi:
            # A már suggeszált határok túl közel → nincs érvényes érték; essünk
            # vissza a teljes tartományra (ritka; a constraints-szűrő elkapja).
            lo, hi = spec["min"], spec["max"]
        if isinstance(spec["min"], int) and isinstance(spec["max"], int) and isinstance(step, int):
            step_i = max(1, int(step))
            hi = int(lo) + ((int(hi) - int(lo)) // step_i) * step_i   # rácsra igazít
            params[key] = trial.suggest_int(key, int(lo), int(hi), step=step_i)
        else:
            params[key] = trial.suggest_float(key, float(lo), float(hi), step=float(step))
    return params


def optimize_pair_optuna(
    symbol: str,
    df_m15: pd.DataFrame,
    df_m1: pd.DataFrame,
    opt_cfg: dict,
    base_params: dict,
    pair_cfg: dict,
    trading_cfg: dict,
    initial_balance: float,
    strategy,
    n_trials: int = 500,
    n_splits: int = 4,
    train_months: int = 6,
    test_months: int = 2,
    progress_callback=None,
) -> Optional[dict]:
    """
    Optuna Bayesian optimalizálás walk-forward validációval.
    A legjobb paramétereket az összes walk-forward ablak átlagos score-ja alapján választja.
    """
    from trading.backtest import run_pair

    # Opt-in: az rr (kockázatcsökkentés) is optimalizált dimenzió? Alapból NEM →
    # a keresési tér és a viselkedés bitazonos a korábbival.
    optimize_rr = bool(opt_cfg.get("optimize_rr", False))

    # Deklaratív paraméter-kényszerek indításkori ellenőrzése: az elgépelt vagy
    # ismeretlen nevű kifejezéseket LOGBA jelezzük (a check() futásidőben kihagyja).
    _cons = opt_cfg.get("constraints", [])
    if _cons:
        from core import param_constraints
        _known = set(base_params) | {k for k, v in opt_cfg.items()
                                     if isinstance(v, dict) and "min" in v}
        for _expr, _why in param_constraints.validate(_cons, _known):
            log.warning("%s — hibás paraméter-kényszer (kihagyva) %r: %s",
                        symbol, _expr, _why)

    windows = _walk_forward_windows(df_m15, n_splits, train_months, test_months)
    if not windows:
        log.warning("%s — nincs elég adat walk-forward ablakokhoz.", symbol)
        return None

    log.info("  Walk-forward: %d ablak (%d hó train + %d hó test)", len(windows), train_months, test_months)
    for i, w in enumerate(windows):
        log.info("    Ablak %d: %s → TRAIN → %s → TEST → %s",
                 i + 1,
                 str(w["train_start"])[:10],
                 str(w["test_start"])[:10],
                 str(w["test_end"])[:10])

    call_count = [0]
    best_score_so_far = [-float("inf")]

    def _record_trial(trial, params, score, summary=None, note="", rr=None):
        """Egy trial sora a CSV-hez — MINDEN trialról (érvénytelen/0-trade is),
        hogy az eredménytáblázat mindig létrejöjjön és lássék, mi történt.
        A sort a TRIAL user_attr-jébe tesszük (a study-val perzisztálódik → a CSV
        a study-ból bármikor újraépíthető, folytatás után is).
        note: elbukás oka (pl. hiányzó config-kulcs), hogy a CSV-ből kiderüljön.
        rr: az adott trial rr-spec-je (ha optimize_rr) → külön oszlopokban."""
        row = {"score": round(score, 2) if score > -999999.0 else score}
        if summary:
            pf = summary["profit_factor"]
            row.update({
                "trades":        summary["trades"],
                "win_rate":      round(summary["win_rate"], 4),
                "total_pnl":     round(summary["total_pnl"], 2),
                "max_drawdown":  round(summary["max_drawdown"], 4),
                "profit_factor": round(pf, 3) if pf != float("inf") else "inf",
            })
        else:
            row["trades"] = 0
        row["note"] = note
        for pk, pv in params.items():
            if not pk.startswith("_"):
                row[pk] = pv
        if optimize_rr and rr:
            row["rr_preset"] = rr.get("preset", "off")
            row["rr_runner"] = rr.get("runner_stop", "")
            row["rr_trigger_R"] = rr.get("trigger_R", "")
            row["rr_halving_fraction"] = rr.get("halving_fraction", "")
            row["rr_shield_fraction"] = rr.get("shield_fraction", "")
        trial.set_user_attr("row", row)

    def objective(trial):
        # ── Haladás MINDEN trialnál, a korai return ELŐTT ──────────────────
        # (Különben a sok érvénytelen trial esetén — pl. BTCUSD — a GUI
        #  stall-timeoutot dob, a CLI nem mutat haladást, és CSV sem készül.)
        call_count[0] += 1
        # Haladás MINDEN trialnál → a GUI stall-órája minden trial után újraindul,
        # így a stall-ablaknak elég EGY trialt lefednie (nem 10-et). A napló
        # viszont csak 10-esével ír, hogy ne árassza el.
        if progress_callback:
            progress_callback(call_count[0], n_trials, best_score_so_far[0])
        if call_count[0] == 1 or call_count[0] % 10 == 0:
            log.info("  %s — %d/%d trial | legjobb score: %.2f",
                     symbol, call_count[0], n_trials, best_score_so_far[0])

        params = _suggest_params(trial, opt_cfg, base_params)
        # rr (kockázatcsökkentés) dimenziók — csak ha opt-in (különben None → OFF).
        rr_spec = _suggest_rr(trial, opt_cfg) if optimize_rr else None
        rr_run  = _rr_for_run(rr_spec)
        if not strategy.constraints_ok(params):
            _record_trial(trial, params, -999999.0,
                          note="paraméter-kényszer nem teljesült", rr=rr_spec)
            return -999999.0

        window_scores = []
        combined_test = []          # az összes ablak TEST-trade-jei (CSV metrikákhoz)
        last_err = ""               # utolsó kivétel szövege (diagnosztika a CSV-be)
        for w in windows:
            try:
                # Teljes ablak adat (train + test) — az indikátorok warmuphoz kellenek
                m15_w = df_m15[df_m15.index >= w["train_start"]]
                m1_w  = df_m1[df_m1.index  >= w["train_start"]]

                result = run_pair(
                    symbol, m15_w, m1_w,
                    params, pair_cfg, trading_cfg,
                    initial_balance,
                    test_start=None,  # teljes ablakot futtatjuk
                    strategy=strategy,
                    rr=rr_run,
                )

                # Csak a TEST periódus trade-jeit értékeljük
                test_trades = [
                    t for t in result.closed
                    if t.close_time is not None and t.close_time >= w["test_start"]
                ]
                combined_test.extend(test_trades)
                window_scores.append(_score_trades(test_trades, initial_balance))

            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                window_scores.append(-999999.0)

        valid = [s for s in window_scores if s > -999999.0]
        if not valid:
            # Ha kivétel volt → az az ok; ha nem, akkor tényleg nincs értékelhető trade.
            _record_trial(trial, params, -999999.0, rr=rr_spec,
                          note=last_err or "nincs értékelhető trade a TEST ablakokban")
            return -999999.0

        # Átlag × konzisztencia arány (hány ablak működött)
        avg_score   = float(np.mean(valid))
        consistency = len(valid) / len(window_scores)
        final_score = avg_score * consistency

        # ── Sor az eredménytáblázathoz (összes ablak TEST-metrikái + params) ──
        pnl_list = [t.pnl_usd for t in combined_test]
        summary = None
        if pnl_list:
            wins   = [p for p in pnl_list if p > 0]
            losses = [p for p in pnl_list if p <= 0]
            bal = peak = initial_balance
            mdd = 0.0
            for p in pnl_list:
                bal += p
                peak = max(peak, bal)
                mdd  = max(mdd, (peak - bal) / peak if peak > 0 else 0)
            pf = abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else float("inf")
            summary = {
                "trades":        len(pnl_list),
                "win_rate":      len(wins) / len(pnl_list),
                "total_pnl":     sum(pnl_list),
                "max_drawdown":  mdd,
                "profit_factor": pf,
            }
        _record_trial(trial, params, final_score, summary, rr=rr_spec)

        if final_score > best_score_so_far[0]:
            best_score_so_far[0] = final_score

        return final_score

    out_csv     = trials_file(symbol, strategy.name)
    storage_url = f"sqlite:///{study_db(symbol, strategy.name).as_posix()}"

    def _dump_csv(study, _trial=None):
        """A trials CSV újraépítése a study-ból (a trialok user_attr sorai).
        Folytatás után a RÉGI trialok is benne vannak (a .db perzisztálja őket)."""
        rows = [t.user_attrs["row"] for t in study.trials if "row" in t.user_attrs]
        try:
            _write_trials_csv(rows, out_csv)
        except Exception as e:
            log.debug("%s — trials CSV mentés hiba: %s", symbol, e)

    def _incremental_cb(study, trial):
        # Inkrementális CSV-mentés minden 10. trial után. A study MINDEN trialt
        # azonnal a .db-be ír → megszakadáskor sem vész el eredmény, a CSV pedig
        # bármikor újraépíthető belőle.
        if (trial.number + 1) % 10 == 0:
            _dump_csv(study)
        # Leállítás-kérés (GUI STOP gomb → stop-marker): trial-határon állunk le.
        # A kezelést (státusz, takarítás) az optimize_symbol közös útja végzi.
        if stop_marker(symbol, strategy.name).exists():
            study.stop()

    # Folytatás-szemantika:
    #   • előző futás BEFEJEZŐDÖTT (marker fájl van) → FRISS optimalizálás
    #     (a régi .db-t töröljük — még a kapcsolat megnyitása ELŐTT, friss
    #     processzben, így nincs Windows-fájlzár),
    #   • előző futás MEGSZAKADT (nincs marker, de van .db) → FOLYTATÁS.
    done_flag = done_marker(symbol, strategy.name)
    if done_flag.exists():
        try:
            study_db(symbol, strategy.name).unlink(missing_ok=True)
            done_flag.unlink(missing_ok=True)
        except Exception as e:
            log.debug("%s — study reset hiba: %s", symbol, e)

    # Perzisztens study (SQLite) → megszakadás után FOLYTATHATÓ ugyanarra a párra.
    study = optuna.create_study(
        study_name=symbol,
        storage=storage_url,
        load_if_exists=True,
        direction="maximize",
        sampler=optuna.samplers.TPESampler(seed=42),
    )
    done = len(study.trials)
    call_count[0] = done            # a haladás a TELJES készültséget mutassa
    if done:
        try:
            best_score_so_far[0] = study.best_value
        except Exception:
            pass
    remaining = max(0, n_trials - done)
    if remaining > 0:
        if done:
            log.info("  %s — FOLYTATÁS: %d kész trial a study-ban, még %d hátra",
                     symbol, done, remaining)
        study.optimize(objective, n_trials=remaining, show_progress_bar=False,
                       callbacks=[_incremental_cb])
    else:
        log.info("  %s — a study már kész (%d trial). Új futáshoz töröld a .db-t: %s",
                 symbol, done, study_db(symbol, strategy.name).name)

    # Végső, teljes CSV a study-ból (a folytatott trialokkal együtt).
    _dump_csv(study)
    log.info("  %s — %d trial eredménye mentve: %s", symbol, len(study.trials), out_csv.name)

    # Ha elértük a teljes trial-számot → BEFEJEZETT: marker, hogy a KÖVETKEZŐ OPT
    # frissen induljon (ne folytassa a már kész study-t).
    if len(study.trials) >= n_trials:
        try:
            done_flag.touch()
        except Exception:
            pass

    if progress_callback:
        progress_callback(n_trials, n_trials, study.best_value)

    if study.best_value <= -999999.0:
        return None

    best_params = _suggest_params(study.best_trial, opt_cfg, base_params)
    # A nyertes trial rr-je (ugyanabból a trialból visszafejtve) — a train/TEST
    # validáció is EZZEL fut, hogy a mentett minősítés konzisztens legyen.
    best_rr     = _suggest_rr(study.best_trial, opt_cfg) if optimize_rr else None
    best_rr_run = _rr_for_run(best_rr)

    # TRAIN summary a teljes train perióduson (utolsó ablak train_start → test_start)
    last_window = windows[-1]
    try:
        from trading.backtest import run_pair as _rp
        m15_tr = df_m15[df_m15.index >= windows[0]["train_start"]]
        m1_tr  = df_m1[df_m1.index  >= windows[0]["train_start"]]
        train_result = _rp(symbol, m15_tr, m1_tr, best_params, pair_cfg, trading_cfg,
                           initial_balance, strategy=strategy, rr=best_rr_run)
        train_trades = [
            t for t in train_result.closed
            if t.close_time is not None and t.close_time < last_window["test_start"]
        ]
        pnl_list  = [t.pnl_usd for t in train_trades]
        wins      = [p for p in pnl_list if p > 0]
        losses    = [p for p in pnl_list if p <= 0]
        balance   = initial_balance
        peak      = balance
        max_dd    = 0.0
        for p in pnl_list:
            balance += p
            peak = max(peak, balance)
            dd   = (peak - balance) / peak if peak > 0 else 0
            max_dd = max(max_dd, dd)

        train_summary = {
            "symbol":        symbol,
            "trades":        len(train_trades),
            "win_rate":      len(wins) / len(train_trades) if train_trades else 0,
            "total_pnl":     sum(pnl_list),
            "max_drawdown":  max_dd,
            "profit_factor": abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else float("inf"),
            "wf_score":      study.best_value,
            "wf_windows":    len(windows),
        }
    except Exception as e:
        log.warning("  %s — train summary hiba: %s", symbol, e)
        train_summary = {"symbol": symbol, "trades": 0, "wf_score": study.best_value}

    return {"params": best_params, "train_summary": train_summary, "rr": best_rr}


# ---------------------------------------------------------------------------
# Egy pár optimalizálása
# ---------------------------------------------------------------------------

def optimize_pair(
    symbol: str,
    df_m15,
    df_m1,
    params_list: list[dict],
    pair_cfg: dict,
    trading_cfg: dict,
    initial_balance: float,
    train_end: str,
    strategy,
    progress_callback=None,   # fn(done: int, total: int, best_pnl: float)
) -> Optional[dict]:
    """
    Végigpróbálja az összes params kombinációt TRAIN adaton.
    Visszaadja a legjobb params dict-et és a hozzá tartozó train summary-t.
    """
    best_score = -float("inf")
    best_params = None
    best_summary = None
    all_rows: list[dict] = []   # minden kombináció eredménye → CSV export

    for i, params in enumerate(params_list):
        # Leállítás-kérés (GUI STOP gomb → stop-marker): kombináció-határon le.
        if stop_marker(symbol, strategy.name).exists():
            log.info("  %s — leállítás-kérés, a keresés megszakítva (%d/%d).",
                     symbol, i, len(params_list))
            break
        try:
            result = run_pair(
                symbol, df_m15, df_m1,
                params, pair_cfg, trading_cfg,
                initial_balance,
                test_start=None,   # TRAIN: teljes adat a train_end-ig
                strategy=strategy,
            )
            # TRAIN adatra szűrünk
            train_result_trades = [
                t for t in result.closed
                if t.close_time is not None and str(t.close_time.date()) < train_end
            ]
            if not train_result_trades:
                continue

            # Gyors summary a train szegmensre
            pnl_list = [t.pnl_usd for t in train_result_trades]
            wins = [p for p in pnl_list if p > 0]
            losses = [p for p in pnl_list if p <= 0]

            balance = initial_balance
            peak = balance
            max_dd = 0.0
            for p in pnl_list:
                balance += p
                peak = max(peak, balance)
                dd = (peak - balance) / peak if peak > 0 else 0
                max_dd = max(max_dd, dd)

            summary = {
                "symbol": symbol,
                "trades": len(train_result_trades),
                "win_rate": len(wins) / len(train_result_trades),
                "total_pnl": sum(pnl_list),
                "max_drawdown": max_dd,
                "profit_factor": abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else float("inf"),
            }

            s = score(summary)

            # Sor az eredménytáblázathoz: score + metrikák + a próbált paraméterek
            row = {
                "score":         round(s, 2),
                "trades":        summary["trades"],
                "win_rate":      round(summary["win_rate"], 4),
                "total_pnl":     round(summary["total_pnl"], 2),
                "max_drawdown":  round(summary["max_drawdown"], 4),
                "profit_factor": (round(summary["profit_factor"], 3)
                                  if summary["profit_factor"] != float("inf") else "inf"),
            }
            for pk, pv in params.items():
                if not pk.startswith("_"):
                    row[pk] = pv
            all_rows.append(row)

            if s > best_score:
                best_score = s
                best_params = params
                best_summary = summary

        except Exception as e:
            log.debug("%s — kombináció hiba: %s", symbol, e)
            continue

        # Haladás MINDEN kombinációnál (stall-óra újraindítás); log 10-esével.
        best_pnl = best_summary["total_pnl"] if best_summary else 0
        if progress_callback:
            progress_callback(i + 1, len(params_list), best_pnl)
        if (i + 1) % 10 == 0:
            log.info(
                "  %s — %d/%d próbált | legjobb P&L: %.2f$",
                symbol, i + 1, len(params_list), best_pnl,
            )
            # Inkrementális CSV: az eddigi eredmények azonnal lemezre (nem vész el).
            _write_trials_csv(all_rows, trials_file(symbol, strategy.name))

    # Végső callback
    if progress_callback:
        best_pnl = best_summary["total_pnl"] if best_summary else 0
        progress_callback(len(params_list), len(params_list), best_pnl)

    # ── Teljes eredménytáblázat mentése CSV-be (score szerint csökkenő) ──────
    n = _write_trials_csv(all_rows, trials_file(symbol, strategy.name))
    if n:
        log.info("  %s — %d kombináció eredménye mentve: %s",
                 symbol, n, trials_file(symbol, strategy.name).name)

    return {"params": best_params, "train_summary": best_summary} if best_params else None


# ---------------------------------------------------------------------------
# Fő belépési pont
# ---------------------------------------------------------------------------

def optimize_symbol(symbol, df_m15, df_m1, cfg, initial_balance, progress=None,
                    strategy=None) -> dict:
    """EGYSÉGES optimalizálási belépési pont — a CLI és a GUI-processz is EZT hívja.

    A method-döntés (optuna | grid | random) EGYETLEN helyen él, így a két felület
    sosem csúszhat szét. Az adat szeletelése train_start-tól, a trials CSV kiírása
    (a compute-függvényekben) és az out-of-sample teszt is itt, egységesen történik.

    strategy: a használandó stratégia (seam). None → a config alapján (get_strategy).
    progress: opcionális fn(done, total, best) haladásjelző.
    Visszaad: {"train_summary","test_summary","params"} vagy {"error": "..."}.
    """
    if strategy is None:
        from strategy import get_strategy
        strategy = get_strategy(cfg)

    # A cfg átképezése a JOB stratégiájának nézetére: a futásidejű cfg az
    # ELSŐDLEGES stratégia szekcióival van merge-elve — másodlagos stratégia
    # optimalizálásakor annak a SAJÁT indicators/sltp/optimizer-tere kell
    # (különben a base_params a másik stratégia kulcsait kapná).
    from strategy.settings import config_for_strategy
    cfg = config_for_strategy(cfg, strategy.name)

    # Stratégia-hatókörű tárolás: az aktív stratégiát beállítjuk (a subprocess is
    # ezt hívja) + egyszeri migráció a régi lapos elrendezésről.
    set_active_strategy(strategy.name)
    migrate_flat_layout(strategy.name)

    opt_cfg     = cfg["optimizer"]
    method      = opt_cfg.get("method", "random")
    max_trials  = opt_cfg.get("max_trials", 500)
    train_start = opt_cfg.get("train_start_date", "2025-01-01")
    test_start  = opt_cfg.get("test_start_date", "2025-10-01")
    trading_cfg = cfg["trading"]
    pair_cfg    = cfg["pairs"][symbol]
    base_params = strategy.base_params(cfg)

    # A tanítható ág (lentebb) a TELJES előzményt kapja — a modell-tanítás a saját
    # lookback-jét alkalmazza (optimizer.training.lookback_years), nem a
    # train_start_date-et (több adat = jobb modell).
    df_m15_full = df_m15

    # Adat szeletelése train_start-tól (idempotens, ha a hívó már szeletelt)
    ts_train = pd.Timestamp(train_start)
    if df_m15.index.tzinfo is not None:
        ts_train = ts_train.tz_localize("UTC")
    df_m15 = df_m15[df_m15.index >= ts_train]
    df_m1  = df_m1[df_m1.index  >= ts_train]
    if len(df_m15) < 200 or len(df_m1) < 200:
        return {"error": "túl kevés adat a train_start után"}

    # OOS-kapu: ha a test_start_date az adat végén túl van (pl. jövőbeli dátum a
    # configban), az out-of-sample szelet ÜRES lenne → az optimizer némán 0-trade
    # test_summary-t mentene (nincs Minőség, a param-ablak "0 trade"-et mutat).
    # Ilyenkor az utolsó wf_test_months hónapra esünk vissza, és naplózzuk.
    ts_test = pd.Timestamp(test_start)
    if df_m15.index.tzinfo is not None:
        ts_test = ts_test.tz_localize("UTC")
    data_end = df_m15.index[-1]
    if ts_test >= data_end:
        _fb = data_end - pd.DateOffset(months=int(opt_cfg.get("wf_test_months", 2)))
        log.warning("  %s — test_start_date (%s) az adat vége (%s) UTÁN van → "
                    "OOS fallback: %s", symbol, test_start,
                    data_end.date(), _fb.date())
        test_start = _fb.strftime("%Y-%m-%d")

    # ── Method-dispatch (EGY helyen) ─────────────────────────────────────────
    # (A stop-marker takarítása a KÉRÉSKOR történik — request_optimize / CLI —,
    # itt nem törlünk: az adat-előkészítés alatt kért STOP-nak is élnie kell.)
    if callable(getattr(strategy, "fit", None)):
        # Tanítható stratégia (pl. ml_ai): az „optimalizálás" = MODELL-TANÍTÁS.
        # A fit a teljes előzményből tanít a test_start ELŐTTI adaton, menti a
        # modellt, és {"params","train_summary"}-t ad — az OOS teszt (lentebb)
        # és a mentés a KÖZÖS úton megy, mint a paraméter-keresésnél.
        log.info("  Tanítható stratégia (%s) → modell-tanítás...", strategy.name)
        done_flag = done_marker(symbol, strategy.name)
        done_flag.unlink(missing_ok=True)          # friss futás — friss marker
        try:
            result = strategy.fit(symbol, df_m15_full, cfg, pair_cfg,
                                  test_start=test_start, progress_callback=progress)
        except RuntimeError as ex:                 # tanítás megszakítva (stop marker)
            log.info("  %s — tanítás megszakítva: %s", symbol, ex)
            result = None
        if result is not None and "error" not in result:
            done_flag.touch()                      # 'Utolsó opt:' címke + állapot
    elif method == "optuna" and _OPTUNA_AVAILABLE:
        log.info("  Optuna Bayesian optimalizálás (%d trial, walk-forward)...", max_trials)
        result = optimize_pair_optuna(
            symbol, df_m15, df_m1, opt_cfg, base_params, pair_cfg, trading_cfg,
            initial_balance, strategy,
            n_trials=max_trials,
            n_splits=opt_cfg.get("wf_n_splits", 4),
            train_months=opt_cfg.get("wf_train_months", 6),
            test_months=opt_cfg.get("wf_test_months", 2),
            progress_callback=progress)
    elif method == "grid":
        params_list = generate_grid_params(opt_cfg, base_params, strategy.constraints_ok)
        log.info("  Grid search: %d kombináció", len(params_list))
        result = optimize_pair(symbol, df_m15, df_m1, params_list, pair_cfg,
                               trading_cfg, initial_balance, test_start, strategy,
                               progress_callback=progress)
    else:
        params_list = generate_random_params(opt_cfg, base_params, max_trials,
                                             strategy.constraints_ok)
        log.info("  Random search: %d kombináció", len(params_list))
        result = optimize_pair(symbol, df_m15, df_m1, params_list, pair_cfg,
                               trading_cfg, initial_balance, test_start, strategy,
                               progress_callback=progress)

    # ── Leállítás-kérés (GUI STOP): a futás eredményét ELDOBJUK ─────────────
    # A user-cancel nem hiba és nem „megszakadt futás": a meglévő mentett
    # paraméterek érintetlenek maradnak, és az induláskori auto-folytatás sem
    # veszi fel újra (a study lezárva/törölve).
    _stop_p = stop_marker(symbol, strategy.name)
    if _stop_p.exists():
        _stop_p.unlink(missing_ok=True)
        try:
            import gc
            gc.collect()                      # SQLite-kapcsolat elengedése (Windows-zár)
            study_db(symbol, strategy.name).unlink(missing_ok=True)
        except Exception:
            # Ha a .db zárolt, a done-marker akadályozza az auto-folytatást;
            # a KÖVETKEZŐ Opt friss study-val indul (done+db → reset).
            try:
                done_marker(symbol, strategy.name).touch()
            except Exception:
                pass
        log.info("  %s — optimalizálás MEGSZAKÍTVA (user stop), eredmény eldobva.",
                 symbol)
        return {"error": "megszakítva", "stopped": True}

    if result is None:
        return {"error": "nincs eredmény"}

    # Out-of-sample (TEST) validálás — szintén itt, egységesen. A nyertes rr-rel
    # (ha volt rr-optimalizálás), hogy a mentett test_summary konzisztens legyen.
    _best_rr = result.get("rr")   # csak az optuna-ág adja; grid/random → None (OFF)
    try:
        test_result  = run_pair(symbol, df_m15, df_m1, result["params"],
                                pair_cfg, trading_cfg, initial_balance,
                                test_start=test_start, strategy=strategy,
                                rr=_rr_for_run(_best_rr))
        test_summary = test_result.summary(initial_balance)
    except Exception as e:
        log.warning("  %s — TEST hiba: %s", symbol, e)
        test_summary = {}

    return {
        "train_summary": result["train_summary"],
        "test_summary":  test_summary,
        "params":        result["params"],
        "rr":            _best_rr,
    }


def apply_optimized_rr(symbol: str, rr: dict):
    """A nyertes rr-t a per-pár állapotba írja (data/risk_mode.json) → a live/GUI
    ezt veszi át (mint az optimalizált paramétereket). Naplózza az alkalmazást."""
    if not rr:
        return
    try:
        from core import rr_state
        rr_state.set_from_optimizer(symbol, rr)
        log.info("  %s — rr alkalmazva a live-ra: preset=%s runner=%s "
                 "trigger_R=%s halving=%s shield=%s", symbol, rr.get("preset"),
                 rr.get("runner_stop"), rr.get("trigger_R"),
                 rr.get("halving_fraction"), rr.get("shield_fraction"))
    except Exception as e:
        log.warning("  %s — rr_state alkalmazás hiba: %s", symbol, e)


def run_optimizer(cfg: dict, symbols: Optional[list[str]] = None):
    opt_cfg     = cfg["optimizer"]
    method      = opt_cfg.get("method", "random")
    max_trials  = opt_cfg.get("max_trials", 500)
    initial_balance = cfg.get("ml", {}).get("starting_balance_eur", 1000.0)

    # Stratégia-hatókörű tárolás: aktív stratégia + egyszeri migráció.
    from strategy.settings import strategy_name as _stratname
    _sn = _stratname(cfg)
    set_active_strategy(_sn)
    migrate_flat_layout(_sn)

    # Párok kiválasztása
    all_pairs = {s: p for s, p in cfg["pairs"].items() if isinstance(p, dict) and p.get("enabled", False)}
    if symbols:
        all_pairs = {s: p for s, p in all_pairs.items() if s in symbols}

    # Meglévő per-pár fájlok listázása (folytatás)
    existing = [f.stem for f in strategy_dir(_sn).glob("*.json")]
    if existing:
        log.info("Meglévő optimalizált párok: %s", ", ".join(existing))

    log.info("Optimalizálás indul | módszer: %s | max_trials: %d | párok: %d",
             method, max_trials, len(all_pairs))

    for symbol, pair_cfg in all_pairs.items():
        # Ha már van mentett eredmény és nem kényszerített újrafuttatás → kihagyás
        if params_file(symbol).exists() and not symbols:
            log.info("─" * 60)
            log.info("✓  %s — már optimalizálva, kihagyva. (Felülíráshoz: python main.py optimize %s)", symbol, symbol)
            continue

        log.info("─" * 60)
        log.info("▶  %s optimalizálása...", symbol)
        t0 = time.time()
        # Elavult (GUI-s) leállítás-marker törlése — a CLI-futást ne szakítsa meg.
        stop_marker(symbol, _sn).unlink(missing_ok=True)

        df_m15, df_m1 = load_data(symbol)
        if df_m15 is None:
            log.warning("%s — nincs adat, kihagyva.", symbol)
            continue

        # ── KÖZÖS dispatch (ugyanaz, mint a GUI-ban) ──────────────────────
        result = optimize_symbol(symbol, df_m15, df_m1, cfg, initial_balance)
        if "error" in result:
            log.warning("%s — %s", symbol, result["error"])
            continue

        train_summary = result["train_summary"]
        test_summary  = result.get("test_summary", {})
        elapsed = time.time() - t0
        log.info(
            "  TRAIN | Kötések: %d | Win: %.0f%% | P&L: %.2f$ | MaxDD: %.1f%%",
            train_summary.get("trades", 0),
            train_summary.get("win_rate", 0) * 100,
            train_summary.get("total_pnl", 0),
            train_summary.get("max_drawdown", 0) * 100,
        )
        if test_summary:
            log.info(
                "  TEST  | Kötések: %d | Win: %.0f%% | P&L: %.2f$ | MaxDD: %.1f%%",
                test_summary.get("trades", 0),
                test_summary.get("win_rate", 0) * 100,
                test_summary.get("total_pnl", 0),
                test_summary.get("max_drawdown", 0) * 100,
            )
        log.info("  ⏱  %.1f mp", elapsed)

        entry = {
            "symbol":        symbol,
            "optimized_at":  datetime.utcnow().isoformat(),
            "train_summary": train_summary,
            "test_summary":  test_summary,
            "params":        result["params"],
        }
        _rr = result.get("rr")
        if _rr:
            entry["rr"] = _rr
        out = params_file(symbol)
        tmp = out.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(entry, f, indent=2, ensure_ascii=False, default=str)
        tmp.replace(out)
        log.info("  Mentve: %s", out)
        if _rr:
            apply_optimized_rr(symbol, _rr)

    log.info("=" * 60)
    log.info("Optimalizálás kész. Eredmények: %s", strategy_dir(_sn))

    # Összesített kimutatás
    log.info("%-10s  %6s  %6s  %8s  %8s", "Szimbólum", "Kötés", "Win%", "P&L$", "MaxDD%")
    log.info("-" * 50)
    for f in sorted(strategy_dir(_sn).glob("*.json")):
        with open(f, encoding="utf-8") as fh:
            data = json.load(fh)
        ts = data.get("test_summary", {})
        log.info(
            "%-10s  %6d  %5.0f%%  %8.2f  %7.1f%%",
            data.get("symbol", f.stem),
            ts.get("trades", 0),
            ts.get("win_rate", 0) * 100,
            ts.get("total_pnl", 0),
            ts.get("max_drawdown", 0) * 100,
        )


# ---------------------------------------------------------------------------
# Külön PROCESSZBEN futtatható feladat (GIL-mentes — a GUI sosem fagy tőle)
# ---------------------------------------------------------------------------

def optimize_job(symbol, df_m15, df_m1, cfg, initial_balance, progress_q=None,
                 strategy_name=None) -> dict:
    """Az `optimize_symbol` PROCESSZBEN futtatható burka (a GUI ezt küldi a pool-nak).

    Minden bemenet picklezhető (DataFrame-ek + a teljes cfg dict), nincs MT5 vagy
    tkinter függés. A haladást a progress_q-ra teszi (symbol, done, total) hármasként.
    A method-döntést (optuna|grid|random) az optimize_symbol intézi → a GUI és a CLI
    UGYANAZT az utat járja. Visszaad: {"train_summary","test_summary","params"} | {"error"}.

    `strategy_name`: MELYIK stratégiát optimalizáljuk (picklázható név, a subprocess
    a `get_strategy_by_name`-mel oldja fel). None → a config alapértelmezett stratégiája
    (visszafelé kompatibilis)."""
    def _progress(done, total, best):
        if progress_q is not None:
            try:
                progress_q.put((symbol, done, total))
            except Exception:
                pass

    try:
        strategy = None
        if strategy_name:
            from strategy import get_strategy_by_name
            strategy = get_strategy_by_name(strategy_name)
        return optimize_symbol(symbol, df_m15, df_m1, cfg, initial_balance,
                               progress=_progress, strategy=strategy)
    except Exception as e:
        import traceback
        return {"error": f"{e}", "traceback": traceback.format_exc()}


if __name__ == "__main__":
    cfg_path = ROOT / "config.json"
    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)

    # Opcionálisan: csak megadott szimbólumok optimalizálása
    # python ml/optimizer.py EURUSD GBPJPY
    symbols = sys.argv[1:] if len(sys.argv) > 1 else None
    run_optimizer(cfg, symbols)
