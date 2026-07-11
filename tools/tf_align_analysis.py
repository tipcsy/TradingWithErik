"""
TF-összhang teszt: a TÖBB-IDŐSÍKÚ trend-egyezés szétválasztja-e a nyerő/vesztő
wpr_sma-kötéseket? (A Tanulóklub „minden idősík egyezik" intuíciója, számszerűen.)

Hipotézis (mean-reversion stratégiára): ha M5/M15/H1 MIND egy irányba mutat (erős
összhang-trend), és a kötés EZ ELLEN fade-el → veszélyes → rosszabb R. Ha az
idősíkok nem egyeznek (vegyes), a fade biztonságosabb.

Minden kötést besorol a belépéskori idősík-egyezés szerint:
  against_aligned : M5/M15/H1 mind egy irányba, a kötés ELLENE fade-el
  with_aligned    : mind egy irányba, a kötés VELE
  mixed           : az idősíkok NEM mind egyeznek
majd IS/OOS bontásban csoportonként mean-R + a kulcs-különbség (against − mixed)
95% CI-vel. A trend-irány idősíkonként: sign(close − SMA(n)).

Futtatás:
    python tools/tf_align_analysis.py --symbol EURUSD
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))

from strategy.settings import load_config
from strategy import get_strategy
from core.params_store import params_file, set_active_strategy
from trading.backtest import load_data, run_pair

_SMA_N = 50    # trend-irány idősíkonként: close vs SMA(_SMA_N)


def _resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    return pd.DataFrame({
        "open":  df["open"].resample(rule).first(),
        "high":  df["high"].resample(rule).max(),
        "low":   df["low"].resample(rule).min(),
        "close": df["close"].resample(rule).last(),
    }).dropna()


def _trend_sign(df: pd.DataFrame, n: int = _SMA_N) -> pd.Series:
    sma = df["close"].rolling(n).mean()
    return np.sign(df["close"] - sma)


def _mean_se(rs):
    n = len(rs)
    if n == 0:
        return 0.0, 0.0, 0
    m = sum(rs) / n
    se = (math.sqrt(sum((r - m) ** 2 for r in rs) / (n - 1)) / math.sqrt(n)) if n > 1 else 0.0
    return m, se, n


def _params_for(symbol, cfg, strategy):
    pf = params_file(symbol, strategy.name)
    if pf.exists():
        with open(pf, encoding="utf-8") as f:
            return json.load(f).get("params", {}) or strategy.base_params(cfg)
    return strategy.base_params(cfg)


def analyze(symbol, cfg, strategy, ib, oos_frac=0.4, with_m1=False):
    df15, df1 = load_data(symbol)
    if df15 is None:
        print(f"  {symbol}: nincs adat.")
        return
    pair_cfg = cfg.get("pairs", {}).get(symbol)
    if not isinstance(pair_cfg, dict):
        print(f"  {symbol}: nincs pár-config.")
        return

    # Idősík-jelek: M5 (M1-ből), M15 (nyers), H1 (M1-ből). Opcionálisan M1 (zajos).
    sm5  = _trend_sign(_resample(df1, "5min")).sort_index()
    sm15 = _trend_sign(df15).sort_index()
    sh1  = _trend_sign(_resample(df1, "60min")).sort_index()
    sm1  = _trend_sign(df1).sort_index() if with_m1 else None

    split_ts = df15.index[int(len(df15) * (1 - oos_frac))]
    params = _params_for(symbol, cfg, strategy)
    res = run_pair(symbol, df15, df1, params, pair_cfg, cfg["trading"], ib,
                   strategy=strategy)
    trades = [t for t in res.closed
              if t.close_time is not None and (getattr(t, "risk_usd", 0) or 0) > 0]
    if len(trades) < 200:
        print(f"  {symbol}: kevés kötés ({len(trades)}).")
        return

    groups = {"against_aligned": {"IS": [], "OOS": []},
              "with_aligned":    {"IS": [], "OOS": []},
              "mixed":           {"IS": [], "OOS": []}}
    for t in trades:
        ot = t.open_time
        signs = [sm5.asof(ot), sm15.asof(ot), sh1.asof(ot)]
        if with_m1:
            signs.append(sm1.asof(ot))
        d = 1 if t.direction == "BUY" else -1
        if all(s == 1 for s in signs):
            grp = "with_aligned" if d == 1 else "against_aligned"
        elif all(s == -1 for s in signs):
            grp = "with_aligned" if d == -1 else "against_aligned"
        else:
            grp = "mixed"
        r = t.pnl_usd / t.risk_usd
        groups[grp]["IS" if ot < split_ts else "OOS"].append(r)

    n_tf = "M1/M5/M15/H1" if with_m1 else "M5/M15/H1"
    n_oos = sum(len(g["OOS"]) for g in groups.values())
    print(f"\n  {symbol}  |  OOS {n_oos} kötés  |  idősíkok: {n_tf}  |  OOS-tól: {split_ts.date()}")
    print(f"  {'Csoport':<17} {'IS_n':>5} {'IS_R':>6}  {'OOS_n':>5} {'OOS_R':>6} {'OOS 95%CI':>16}  {'megoszl.':>8}")
    print("  " + "-" * 76)
    order = ["against_aligned", "mixed", "with_aligned"]
    stats = {}
    for g in order:
        im, _, inn = _mean_se(groups[g]["IS"])
        om, ose, onn = _mean_se(groups[g]["OOS"])
        stats[g] = (om, ose, onn)
        share = onn / n_oos * 100 if n_oos else 0
        ci = f"[{om-1.96*ose:+.2f},{om+1.96*ose:+.2f}]"
        print(f"  {g:<17} {inn:>5} {im:>+6.2f}  {onn:>5} {om:>+6.2f} {ci:>16}  {share:>6.0f}%")

    # Kulcs-teszt: against_aligned vs mixed (a hipotézis: against ROSSZABB)
    (am, ase, an) = stats["against_aligned"]
    (mm, mse, mn) = stats["mixed"]
    if an >= 30 and mn >= 30:
        spread = am - mm
        ci = 1.96 * math.sqrt(ase**2 + mse**2)
        sig = "SZIGNIFIKÁNS" if abs(spread) > ci else "nem szign."
        verdict = ("hipotézis IGAZOLVA (against rosszabb)"
                   if (spread < 0 and abs(spread) > ci) else
                   "hipotézis CÁFOLVA (against jobb)" if (spread > 0 and abs(spread) > ci)
                   else "nincs szign. különbség")
        print(f"  → against − mixed OOS: {spread:+.2f}  ±{ci:.2f}  [{sig}]  {verdict}")
    else:
        print(f"  → against_aligned kis minta (n={an}) — nem ítélünk.")


def main():
    ap = argparse.ArgumentParser(description="TF-összhang teszt")
    ap.add_argument("--symbol", default=None)
    ap.add_argument("--oos-frac", type=float, default=0.4)
    ap.add_argument("--with-m1", action="store_true", help="az M1 idősík is (zajos)")
    args = ap.parse_args()
    cfg = load_config(str(ROOT / "config.json"))
    strategy = get_strategy(cfg)
    set_active_strategy(strategy.name)
    ib = float(cfg.get("ml", {}).get("starting_balance_eur", 1000.0))
    symbols = ([args.symbol] if args.symbol else
               [s for s, p in cfg.get("pairs", {}).items()
                if isinstance(p, dict) and p.get("enabled", False)])
    print(f"TF-összhang teszt | stratégia: {strategy.name} | OOS-hányad: {args.oos_frac}"
          f" | SMA{_SMA_N}")
    for sym in symbols:
        try:
            analyze(sym, cfg, strategy, ib, args.oos_frac, args.with_m1)
        except Exception as e:
            import traceback
            print(f"  {sym}: hiba — {e}")
            traceback.print_exc()


if __name__ == "__main__":
    main()
