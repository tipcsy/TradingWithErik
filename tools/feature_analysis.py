"""
Feature-keresés: melyik EGYETLEN piaci változó választja szét legélesebben a
nyerő/vesztő kötéseket OUT-OF-SAMPLE?

A regime (8 kategória) elmoshatja a valódi jelet. Itt FOLYTONOS piaci feature-öket
mérünk: kötésenként a belépéskori (M15) értéket, IS-kvintilisekbe sorolva (OOS-ba
NEM kukucskálunk: az IS-en definiált küszöbökkel), majd OOS-ban kvintilisenként a
mean-R + a legjobb−legrosszabb kvintilis különbsége 95% CI-vel. Ez megmondja, van-e
egy egyszerű, MONOTON, out-of-sample is stabil szeparátor.

Feature-ök (M15, belépéskor):
  atr_ratio  : ATR(14)/SMA(ATR14,100)         — volatilitás-regime
  adx        : Wilder ADX(14)                 — trend-erő
  abs_di     : |+DI−−DI|                       — irány-erő
  ma_dist    : |close−SMA200|/ATR14           — kifeszítettség (szép-chart D_norm)
  atr_pct    : ATR(14)/close                  — nyers normált volatilitás
  with_trend : a kötés iránya EGYEZIK-e a SMA200-trenddel (0/1)

Futtatás:
    python tools/feature_analysis.py --symbol EURUSD
"""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))

from strategy.settings import load_config
from strategy import get_strategy
from core.params_store import params_file, set_active_strategy
from core import regime
from trading.backtest import load_data, run_pair

_MIN_OOS_N = 40   # kvintilisenként ennyi alatt óvatosan


def _params_for(symbol, cfg, strategy):
    pf = params_file(symbol, strategy.name)
    if pf.exists():
        with open(pf, encoding="utf-8") as f:
            return json.load(f).get("params", {}) or strategy.base_params(cfg)
    return strategy.base_params(cfg)


def compute_features(df15: pd.DataFrame) -> pd.DataFrame:
    """A jelölt piaci feature-ök idősorai M15-ön (a regime-modul indikátoraira építve)."""
    feat = regime.features(df15)                    # adx, di_diff, atr_ratio
    close = df15["close"]
    # Wilder ATR14 (a regime-modul belső képletével egyezően)
    prev = close.shift(1)
    tr = pd.concat([df15["high"] - df15["low"], (df15["high"] - prev).abs(),
                    (df15["low"] - prev).abs()], axis=1).max(axis=1)
    atr14 = tr.ewm(alpha=1/14, adjust=False).mean()
    sma200 = close.rolling(200).mean()
    out = pd.DataFrame(index=df15.index)
    out["atr_ratio"] = feat["atr_ratio"]
    out["adx"]       = feat["adx"]
    out["abs_di"]    = feat["di_diff"].abs()
    out["ma_dist"]   = (close - sma200).abs() / atr14.replace(0, np.nan)
    out["atr_pct"]   = atr14 / close.replace(0, np.nan)
    out["ma_sign"]   = np.sign(close - sma200)      # +1 MA fölött, −1 alatt (irányhoz)
    return out


def _mean_se(rs):
    n = len(rs)
    if n == 0:
        return 0.0, 0.0, 0
    m = sum(rs) / n
    se = (math.sqrt(sum((r - m) ** 2 for r in rs) / (n - 1)) / math.sqrt(n)) if n > 1 else 0.0
    return m, se, n


def analyze(symbol, cfg, strategy, ib, oos_frac=0.4):
    df15, df1 = load_data(symbol)
    if df15 is None:
        print(f"  {symbol}: nincs adat.")
        return
    pair_cfg = cfg.get("pairs", {}).get(symbol)
    if not isinstance(pair_cfg, dict):
        print(f"  {symbol}: nincs pár-config.")
        return

    feats = compute_features(df15).sort_index()
    split_ts = df15.index[int(len(df15) * (1 - oos_frac))]
    params = _params_for(symbol, cfg, strategy)
    res = run_pair(symbol, df15, df1, params, pair_cfg, cfg["trading"], ib,
                   strategy=strategy)
    trades = [t for t in res.closed
              if t.close_time is not None and (getattr(t, "risk_usd", 0) or 0) > 0]
    if len(trades) < 200:
        print(f"  {symbol}: kevés kötés ({len(trades)}) — kihagyva.")
        return

    fcols = ["atr_ratio", "adx", "abs_di", "ma_dist", "atr_pct", "with_trend"]
    # Kötésenként: (feature-értékek, R, IS/OOS)
    rows = []
    fser = {c: feats[c] for c in feats.columns}
    for t in trades:
        ot = t.open_time
        vals = {c: fser[c].asof(ot) for c in feats.columns}
        d = 1 if t.direction == "BUY" else -1
        with_trend = 1.0 if (vals["ma_sign"] == d) else 0.0
        r = t.pnl_usd / t.risk_usd
        rows.append({
            "R": r, "is": ot < split_ts,
            "atr_ratio": vals["atr_ratio"], "adx": vals["adx"],
            "abs_di": vals["abs_di"], "ma_dist": vals["ma_dist"],
            "atr_pct": vals["atr_pct"], "with_trend": with_trend,
        })
    dfr = pd.DataFrame(rows).dropna()
    is_df  = dfr[dfr["is"]]
    oos_df = dfr[~dfr["is"]]
    print(f"\n  {symbol}  |  IS {len(is_df)} / OOS {len(oos_df)} kötés  |  OOS-tól: {split_ts.date()}")

    results = []
    for c in fcols:
        if c == "with_trend":
            # bináris feature: 0 vs 1 csoport OOS mean-R
            g0 = oos_df[oos_df[c] == 0]["R"].tolist()
            g1 = oos_df[oos_df[c] == 1]["R"].tolist()
            i0 = is_df[is_df[c] == 0]["R"].mean() if len(is_df[is_df[c]==0]) else 0
            i1 = is_df[is_df[c] == 1]["R"].mean() if len(is_df[is_df[c]==1]) else 0
            m0, se0, n0 = _mean_se(g0)
            m1, se1, n1 = _mean_se(g1)
            spread = m1 - m0
            ci = 1.96 * math.sqrt(se0**2 + se1**2)
            sig = abs(spread) > ci and n0 >= _MIN_OOS_N and n1 >= _MIN_OOS_N
            results.append((c, spread, ci, sig,
                            f"ellen-trend R={m0:+.2f}(n{n0}) | trend-irány R={m1:+.2f}(n{n1})"
                            f"  [IS: {i0:+.2f}|{i1:+.2f}]"))
            continue
        # folytonos: IS-kvintilis küszöbök, majd OOS kvintilisenkénti mean-R
        vis = is_df[c].values
        if len(vis) < 50:
            continue
        edges = np.quantile(vis, [0.2, 0.4, 0.6, 0.8])
        def _q(v):
            return int(np.searchsorted(edges, v, side="right"))
        oos_q = defaultdict(list); is_q = defaultdict(list)
        for _, rr in oos_df.iterrows():
            oos_q[_q(rr[c])].append(rr["R"])
        for _, rr in is_df.iterrows():
            is_q[_q(rr[c])].append(rr["R"])
        q_means_oos = {q: _mean_se(oos_q[q]) for q in range(5)}
        q_means_is  = {q: (sum(is_q[q])/len(is_q[q]) if is_q[q] else 0) for q in range(5)}
        m0, se0, n0 = q_means_oos[0]     # legalsó kvintilis
        m4, se4, n4 = q_means_oos[4]     # legfelső
        spread = m4 - m0
        ci = 1.96 * math.sqrt(se0**2 + se4**2)
        sig = abs(spread) > ci and n0 >= _MIN_OOS_N and n4 >= _MIN_OOS_N
        detail = " ".join(f"Q{q+1}:{q_means_oos[q][0]:+.2f}" for q in range(5))
        results.append((c, spread, ci, sig,
                        f"{detail}  [IS Q1/Q5: {q_means_is[0]:+.2f}/{q_means_is[4]:+.2f}]"))

    # Rangsor: a szignifikáns, legnagyobb |spread| elöl
    results.sort(key=lambda x: (x[3], abs(x[1])), reverse=True)
    print(f"  {'Feature':<11} {'Q5−Q1 spread':>13} {'±CI':>6}  {'szign?':>6}  részletek")
    print("  " + "-" * 100)
    for c, spread, ci, sig, detail in results:
        flag = "IGEN" if sig else "—"
        print(f"  {c:<11} {spread:>+13.2f} {ci:>6.2f}  {flag:>6}  {detail}")


def main():
    ap = argparse.ArgumentParser(description="Feature-keresés IS/OOS")
    ap.add_argument("--symbol", default=None)
    ap.add_argument("--oos-frac", type=float, default=0.4)
    args = ap.parse_args()
    cfg = load_config(str(ROOT / "config.json"))
    strategy = get_strategy(cfg)
    set_active_strategy(strategy.name)
    ib = float(cfg.get("ml", {}).get("starting_balance_eur", 1000.0))
    symbols = ([args.symbol] if args.symbol else
               [s for s, p in cfg.get("pairs", {}).items()
                if isinstance(p, dict) and p.get("enabled", False)])
    print(f"Feature-keresés | stratégia: {strategy.name} | OOS-hányad: {args.oos_frac}")
    for sym in symbols:
        try:
            analyze(sym, cfg, strategy, ib, args.oos_frac)
        except Exception as e:
            import traceback
            print(f"  {sym}: hiba — {e}")
            traceback.print_exc()


if __name__ == "__main__":
    main()
