"""
Regime-elemzés IS/OOS + Wilson-CI: mely piaci kategóriák jók/rosszak a stratégiának
— OUT-OF-SAMPLE is szignifikánsan?

A projekt backtestjét (`run_pair`) futtatja a valós parquet-adaton, minden zárt
kötést megjelöl a belépéskori M15-regime-mel (`core.regime`), majd IS/OOS bontásban
kategóriánként aggregál. Két bizonytalanság-mérőszám:
  • win-rate 95% Wilson-CI (a tk002 módszertana),
  • R-várható-érték (mean R/kötés) 95% CI (normál SE) — EZ a döntő metrika, mert a
    wpr_sma RR-je változó, így a WR önmagában nem elég.

Ítélet: egy kategória csak akkor „ROBUST +/−", ha az OOS mean-R 95% CI NEM lépi át a
0-t ÉS az IS előjele egyezik (nincs regime-túlillesztés a zajra).

Futtatás:
    python tools/regime_analysis.py --symbol EURUSD
    python tools/regime_analysis.py --symbol NAS100 --oos-frac 0.4
"""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))

from strategy.settings import load_config
from strategy import get_strategy
from core.params_store import params_file, set_active_strategy
from core import regime
from trading.backtest import load_data, run_pair

_ORDER = [regime.CLEAN_BULL, regime.CLEAN_BEAR, regime.RANGING, regime.TRANSITION,
          regime.DEAD, regime.VOLATILE_BULL, regime.VOLATILE_BEAR,
          regime.UNCERTAIN, regime.UNCATEGORIZED]

_MIN_OOS_N = 30   # e alatt „kis minta" (nem ítélünk robusztusságot)


def wilson_ci(wins: int, n: int, z: float = 1.96):
    """Wilson score 95% CI a win-rate-re (tk002-vel azonos)."""
    if n == 0:
        return (0.0, 1.0)
    p = wins / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


def _stats(rs: list):
    """Egy R-lista statisztikái: n, WR, mean R + 95% CI (SE), WR Wilson-CI."""
    n = len(rs)
    if n == 0:
        return None
    wins = sum(1 for r in rs if r > 0)
    wr = wins / n
    mean = sum(rs) / n
    if n > 1:
        var = sum((r - mean) ** 2 for r in rs) / (n - 1)
        se = math.sqrt(var) / math.sqrt(n)
    else:
        se = 0.0
    wlo, whi = wilson_ci(wins, n)
    return {"n": n, "wr": wr, "mean": mean,
            "lo": mean - 1.96 * se, "hi": mean + 1.96 * se,
            "wlo": wlo, "whi": whi}


def _verdict(is_s, oos_s) -> str:
    if oos_s is None or oos_s["n"] < _MIN_OOS_N:
        return "kis minta"
    if oos_s["lo"] > 0 and is_s and is_s["mean"] > 0:
        return "ROBUST +"           # OOS is szignifikánsan pozitív → normál/kedvelt
    if oos_s["hi"] < 0 and is_s and is_s["mean"] < 0:
        return "ROBUST −"           # OOS is szignifikánsan negatív → kihagy/óvatos
    if is_s and (is_s["mean"] > 0) != (oos_s["mean"] > 0):
        return "instabil"           # IS/OOS előjel eltér → zaj, ne bízz benne
    return "bizonytalan"            # CI átlép a 0-n


def _params_for(symbol: str, cfg: dict, strategy) -> dict:
    pf = params_file(symbol, strategy.name)
    if pf.exists():
        with open(pf, encoding="utf-8") as f:
            return json.load(f).get("params", {}) or strategy.base_params(cfg)
    return strategy.base_params(cfg)


def analyze(symbol: str, cfg: dict, strategy, ib: float, oos_frac: float = 0.4,
            reg_params: dict | None = None):
    df15, df1 = load_data(symbol)
    if df15 is None:
        print(f"  {symbol}: nincs letöltött adat — kihagyva.")
        return
    pair_cfg = cfg.get("pairs", {}).get(symbol)
    if not isinstance(pair_cfg, dict):
        print(f"  {symbol}: nincs pár-config — kihagyva.")
        return

    reg = regime.classify(df15, reg_params).sort_index()
    # Idő-alapú IS/OOS split: az adat utolsó `oos_frac` hányada az OOS.
    split_ts = df15.index[int(len(df15) * (1 - oos_frac))]

    params = _params_for(symbol, cfg, strategy)
    res = run_pair(symbol, df15, df1, params, pair_cfg, cfg["trading"], ib,
                   strategy=strategy)
    trades = [t for t in res.closed
              if t.close_time is not None and (getattr(t, "risk_usd", 0) or 0) > 0]
    if not trades:
        print(f"  {symbol}: 0 értékelhető kötés.")
        return

    data = {"IS": defaultdict(list), "OOS": defaultdict(list)}
    for t in trades:
        cat = reg.asof(t.open_time)
        if not isinstance(cat, str):
            cat = regime.UNCATEGORIZED
        period = "IS" if t.open_time < split_ts else "OOS"
        data[period][cat].append(t.pnl_usd / t.risk_usd)

    n_is  = sum(len(v) for v in data["IS"].values())
    n_oos = sum(len(v) for v in data["OOS"].values())
    print(f"\n  {symbol}  |  IS {n_is} / OOS {n_oos} kötés  |  OOS-tól: {split_ts.date()}")
    print(f"  {'Kategória':<15} {'IS_n':>5} {'IS_R':>6}  {'OOS_n':>5} {'Win%':>5} "
          f"{'Win-CI':>11} {'OOS_R':>6} {'OOS_R 95%CI':>16}  {'Ítélet':<11}")
    print("  " + "-" * 92)
    for cat in _ORDER:
        is_s  = _stats(data["IS"].get(cat, []))
        oos_s = _stats(data["OOS"].get(cat, []))
        if is_s is None and oos_s is None:
            continue
        isr = f"{is_s['mean']:+.2f}" if is_s else "  —"
        isn = is_s["n"] if is_s else 0
        if oos_s:
            wci = f"[{oos_s['wlo']*100:.0f},{oos_s['whi']*100:.0f}]"
            rci = f"[{oos_s['lo']:+.2f},{oos_s['hi']:+.2f}]"
            row = (f"  {regime.NAME_HU.get(cat, cat):<15} {isn:>5} {isr:>6}  "
                   f"{oos_s['n']:>5} {oos_s['wr']*100:>4.0f}% {wci:>11} "
                   f"{oos_s['mean']:>+6.2f} {rci:>16}  {_verdict(is_s, oos_s):<11}")
        else:
            row = (f"  {regime.NAME_HU.get(cat, cat):<15} {isn:>5} {isr:>6}  "
                   f"{'—':>5} {'—':>5} {'—':>11} {'—':>6} {'—':>16}  {'nincs OOS':<11}")
        print(row)


def main():
    ap = argparse.ArgumentParser(description="Regime-elemzés IS/OOS + Wilson-CI")
    ap.add_argument("--symbol", default=None)
    ap.add_argument("--oos-frac", type=float, default=0.4,
                    help="az adat utolsó ennyi hányada az OOS (alap: 0.4)")
    args = ap.parse_args()

    cfg = load_config(str(ROOT / "config.json"))
    strategy = get_strategy(cfg)
    set_active_strategy(strategy.name)
    ib = float(cfg.get("ml", {}).get("starting_balance_eur", 1000.0))

    symbols = ([args.symbol] if args.symbol else
               [s for s, p in cfg.get("pairs", {}).items()
                if isinstance(p, dict) and p.get("enabled", False)])
    print(f"Regime IS/OOS-elemzés | stratégia: {strategy.name} | OOS-hányad: {args.oos_frac}")
    for sym in symbols:
        try:
            analyze(sym, cfg, strategy, ib, args.oos_frac)
        except Exception as e:
            print(f"  {sym}: hiba — {e}")


if __name__ == "__main__":
    main()
