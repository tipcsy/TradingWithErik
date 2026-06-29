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

PARAMS_DIR = ROOT / "data" / "optimized_params"
PARAMS_DIR.mkdir(parents=True, exist_ok=True)

def params_file(symbol: str) -> Path:
    return PARAMS_DIR / f"{symbol}.json"


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


def generate_random_params(opt_cfg: dict, base_params: dict, n: int) -> list[dict]:
    """N db véletlen paraméter kombinációt generál."""
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

        # Kényszer: sell_extreme > trigger > buy_extreme (SELL logika fordítva)
        # WPR értékek: sell_extreme közel 0-hoz, buy_extreme közel -100-hoz
        if p.get("wpr_m15_sell_extreme", -20) <= p.get("wpr_m15_trigger", -50):
            continue
        if p.get("wpr_m15_trigger", -50) <= p.get("wpr_m15_buy_extreme", -80):
            continue
        if p.get("wpr_m1_sell_extreme", -20) <= p.get("wpr_m1_trigger", -50):
            continue
        if p.get("wpr_m1_trigger", -50) <= p.get("wpr_m1_buy_extreme", -80):
            continue

        key_tuple = tuple(sorted(p.items()))
        if key_tuple in seen:
            continue
        seen.add(key_tuple)
        combos.append(p)

    return combos


def generate_grid_params(opt_cfg: dict, base_params: dict) -> list[dict]:
    """Teljes grid — csak kis paramétertérnél használandó!"""
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

        if p.get("wpr_m15_sell_extreme", -20) <= p.get("wpr_m15_trigger", -50):
            continue
        if p.get("wpr_m15_trigger", -50) <= p.get("wpr_m15_buy_extreme", -80):
            continue
        if p.get("wpr_m1_sell_extreme", -20) <= p.get("wpr_m1_trigger", -50):
            continue
        if p.get("wpr_m1_trigger", -50) <= p.get("wpr_m1_buy_extreme", -80):
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

def _suggest_params(trial, opt_cfg: dict, base_params: dict) -> dict:
    """Optuna trial → paraméter dict."""
    params = deepcopy(base_params)
    for key, spec in opt_cfg.items():
        if not isinstance(spec, dict) or "min" not in spec:
            continue
        lo, hi, step = spec["min"], spec["max"], spec["step"]
        if isinstance(lo, int) and isinstance(hi, int) and isinstance(step, int):
            params[key] = trial.suggest_int(key, lo, hi, step=max(1, int(step)))
        else:
            params[key] = trial.suggest_float(key, float(lo), float(hi), step=float(step))
    return params


def _wpr_constraints_ok(params: dict) -> bool:
    """WPR szint sorrendek ellenőrzése."""
    if params.get("wpr_m15_sell_extreme", -20) <= params.get("wpr_m15_trigger", -50):
        return False
    if params.get("wpr_m15_trigger", -50) <= params.get("wpr_m15_buy_extreme", -80):
        return False
    if params.get("wpr_m1_sell_extreme", -20) <= params.get("wpr_m1_trigger", -50):
        return False
    if params.get("wpr_m1_trigger", -50) <= params.get("wpr_m1_buy_extreme", -80):
        return False
    # Session logika: start < end
    if params.get("trade_hour_start", 0) >= params.get("trade_hour_end", 24):
        return False
    return True


def optimize_pair_optuna(
    symbol: str,
    df_m15: pd.DataFrame,
    df_m1: pd.DataFrame,
    opt_cfg: dict,
    base_params: dict,
    pair_cfg: dict,
    trading_cfg: dict,
    initial_balance: float,
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

    def objective(trial):
        params = _suggest_params(trial, opt_cfg, base_params)
        if not _wpr_constraints_ok(params):
            return -999999.0

        window_scores = []
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
                )

                # Csak a TEST periódus trade-jeit értékeljük
                test_trades = [
                    t for t in result.closed
                    if t.close_time is not None and t.close_time >= w["test_start"]
                ]
                window_scores.append(_score_trades(test_trades, initial_balance))

            except Exception:
                window_scores.append(-999999.0)

        valid = [s for s in window_scores if s > -999999.0]
        if not valid:
            return -999999.0

        # Átlag × konzisztencia arány (hány ablak működött)
        avg_score   = float(np.mean(valid))
        consistency = len(valid) / len(window_scores)
        final_score = avg_score * consistency

        # Progress log minden 50. trialnál
        call_count[0] += 1
        if call_count[0] % 50 == 0:
            if final_score > best_score_so_far[0]:
                best_score_so_far[0] = final_score
            log.info("  %s — %d/%d trial | legjobb score: %.2f",
                     symbol, call_count[0], n_trials, best_score_so_far[0])
            if progress_callback:
                progress_callback(call_count[0], n_trials, best_score_so_far[0])

        return final_score

    study = optuna.create_study(
        direction="maximize",
        sampler=optuna.samplers.TPESampler(seed=42),
    )
    study.optimize(objective, n_trials=n_trials, show_progress_bar=False)

    if progress_callback:
        progress_callback(n_trials, n_trials, study.best_value)

    if study.best_value <= -999999.0:
        return None

    best_params = _suggest_params(study.best_trial, opt_cfg, base_params)

    # TRAIN summary a teljes train perióduson (utolsó ablak train_start → test_start)
    last_window = windows[-1]
    try:
        from trading.backtest import run_pair as _rp
        m15_tr = df_m15[df_m15.index >= windows[0]["train_start"]]
        m1_tr  = df_m1[df_m1.index  >= windows[0]["train_start"]]
        train_result = _rp(symbol, m15_tr, m1_tr, best_params, pair_cfg, trading_cfg, initial_balance)
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

    return {"params": best_params, "train_summary": train_summary}


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
    progress_callback=None,   # fn(done: int, total: int, best_pnl: float)
) -> Optional[dict]:
    """
    Végigpróbálja az összes params kombinációt TRAIN adaton.
    Visszaadja a legjobb params dict-et és a hozzá tartozó train summary-t.
    """
    best_score = -float("inf")
    best_params = None
    best_summary = None

    for i, params in enumerate(params_list):
        try:
            result = run_pair(
                symbol, df_m15, df_m1,
                params, pair_cfg, trading_cfg,
                initial_balance,
                test_start=None,   # TRAIN: teljes adat a train_end-ig
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
            if s > best_score:
                best_score = s
                best_params = params
                best_summary = summary

        except Exception as e:
            log.debug("%s — kombináció hiba: %s", symbol, e)
            continue

        if (i + 1) % 50 == 0:
            best_pnl = best_summary["total_pnl"] if best_summary else 0
            log.info(
                "  %s — %d/%d próbált | legjobb P&L: %.2f$",
                symbol, i + 1, len(params_list), best_pnl,
            )
            if progress_callback:
                progress_callback(i + 1, len(params_list), best_pnl)

    # Végső callback
    if progress_callback:
        best_pnl = best_summary["total_pnl"] if best_summary else 0
        progress_callback(len(params_list), len(params_list), best_pnl)

    return {"params": best_params, "train_summary": best_summary} if best_params else None


# ---------------------------------------------------------------------------
# Fő belépési pont
# ---------------------------------------------------------------------------

def run_optimizer(cfg: dict, symbols: Optional[list[str]] = None):
    opt_cfg     = cfg["optimizer"]
    method      = opt_cfg.get("method", "random")
    max_trials  = opt_cfg.get("max_trials", 500)
    test_start  = opt_cfg.get("test_start_date", "2025-01-01")
    initial_balance = cfg.get("ml", {}).get("starting_balance_eur", 1000.0)
    trading_cfg = cfg["trading"]

    base_params = {
        **cfg["indicators"],
        **cfg["sltp"],
        **cfg["position_mgmt"],
    }

    # Párok kiválasztása
    all_pairs = {s: p for s, p in cfg["pairs"].items() if isinstance(p, dict) and p.get("enabled", False)}
    if symbols:
        all_pairs = {s: p for s, p in all_pairs.items() if s in symbols}

    # Meglévő per-pár fájlok listázása (folytatás)
    existing = [f.stem for f in PARAMS_DIR.glob("*.json")]
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

        df_m15, df_m1 = load_data(symbol)
        if df_m15 is None:
            log.warning("%s — nincs adat, kihagyva.", symbol)
            continue

        # Adatok szeletelése: train_start → train_end (gyorsabb backtest)
        train_start = opt_cfg.get("train_start_date", "2025-01-01")
        ts_train = pd.Timestamp(train_start)
        if df_m15.index.tzinfo is not None:
            ts_train = ts_train.tz_localize("UTC")
        df_m15 = df_m15[df_m15.index >= ts_train].copy()
        df_m1  = df_m1[df_m1.index  >= ts_train].copy()
        if len(df_m15) < 200 or len(df_m1) < 200:
            log.warning("%s — túl kevés adat a train_start után, kihagyva.", symbol)
            continue
        log.info("  Adat: M15=%d gyertya, M1=%d gyertya (%s-tól)",
                 len(df_m15), len(df_m1), train_start)

        # Optimalizálás futtatása a konfigurált módszerrel
        if method == "optuna" and _OPTUNA_AVAILABLE:
            log.info("  Optuna Bayesian optimalizálás (%d trial, walk-forward)...", max_trials)
            result = optimize_pair_optuna(
                symbol, df_m15, df_m1,
                opt_cfg, base_params,
                pair_cfg, trading_cfg,
                initial_balance,
                n_trials=max_trials,
                n_splits=opt_cfg.get("wf_n_splits", 4),
                train_months=opt_cfg.get("wf_train_months", 6),
                test_months=opt_cfg.get("wf_test_months", 2),
            )
        elif method == "grid":
            params_list = generate_grid_params(opt_cfg, base_params)
            log.info("  Grid search: %d kombináció", len(params_list))
            result = optimize_pair(
                symbol, df_m15, df_m1,
                params_list, pair_cfg, trading_cfg,
                initial_balance, test_start,
            )
        else:
            params_list = generate_random_params(opt_cfg, base_params, max_trials)
            log.info("  Random search: %d kombináció", len(params_list))
            result = optimize_pair(
                symbol, df_m15, df_m1,
                params_list, pair_cfg, trading_cfg,
                initial_balance, test_start,
            )

        if result is None:
            log.warning("%s — nem találtunk jó paramétert.", symbol)
            continue

        best_params   = result["params"]
        train_summary = result["train_summary"]

        elapsed = time.time() - t0
        log.info(
            "  TRAIN | Kötések: %d | Win: %.0f%% | P&L: %.2f$ | MaxDD: %.1f%%",
            train_summary["trades"],
            train_summary["win_rate"] * 100,
            train_summary["total_pnl"],
            train_summary["max_drawdown"] * 100,
        )
        log.info("  ⏱  %.1f mp", elapsed)

        # ── AZONNALI MENTÉS a teszt előtt — crash esetén sem vész el ────
        entry = {
            "symbol":        symbol,
            "optimized_at":  datetime.utcnow().isoformat(),
            "train_summary": train_summary,
            "test_summary":  {},   # teszt után frissítjük
            "params":        best_params,
        }
        out = params_file(symbol)
        tmp = out.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(entry, f, indent=2, ensure_ascii=False, default=str)
        tmp.replace(out)
        log.info("  Mentve (train): %s", out)

        # Out-of-sample (TEST) validálás
        try:
            test_result  = run_pair(
                symbol, df_m15, df_m1,
                best_params, pair_cfg, trading_cfg,
                initial_balance, test_start=test_start,
            )
            test_summary = test_result.summary(initial_balance)
            log.info(
                "  TEST  | Kötések: %d | Win: %.0f%% | P&L: %.2f$ | MaxDD: %.1f%%",
                test_summary.get("trades", 0),
                test_summary.get("win_rate", 0) * 100,
                test_summary.get("total_pnl", 0),
                test_summary.get("max_drawdown", 0) * 100,
            )
            # Fájl frissítése a test_summary-vel
            entry["test_summary"] = test_summary
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(entry, f, indent=2, ensure_ascii=False, default=str)
            tmp.replace(out)
        except Exception as te:
            log.warning("  %s — TEST hiba (train eredmény megmarad): %s", symbol, te)
        log.info("  Mentve: %s", out)

    log.info("=" * 60)
    log.info("Optimalizálás kész. Eredmények: %s", PARAMS_DIR)

    # Összesített kimutatás
    log.info("%-10s  %6s  %6s  %8s  %8s", "Szimbólum", "Kötés", "Win%", "P&L$", "MaxDD%")
    log.info("-" * 50)
    for f in sorted(PARAMS_DIR.glob("*.json")):
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


if __name__ == "__main__":
    cfg_path = ROOT / "config.json"
    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)

    # Opcionálisan: csak megadott szimbólumok optimalizálása
    # python ml/optimizer.py EURUSD GBPJPY
    symbols = sys.argv[1:] if len(sys.argv) > 1 else None
    run_optimizer(cfg, symbols)
