"""MT5 backtest-reprodukció — a Python-backtest belépőit a `BacktestReplayer.mq5`
expert által olvasható CSV-be írja (12 oszlop).

Az EA az OPEN-eseményekből nyit, a BE/trail/SL/TP/EOD-t BELÜL kezeli (a
be_trigger / trail_trigger / trail_dist_p alapján, bar H→L logikával), így az MT5
Strategy Tester reprodukálja a Python-eredményt. A modell az OFF preset egyszerű
BE+trail logikájával egyezik (a risky/felező/pajzs preset + kiszállási jel +
pozícióépítés a Python-oldalon van, az EA-ban nem).

Forrás: Trading-with-ai/ml_backtest.py + tools/BacktestReplayer.mq5 (áthozva).
CSV oszlopok: event, datetime, symbol, direction, price, sl, tp, lot, comment,
              be_trigger, trail_trigger, trail_dist_p
Az OPEN időbélyege a belépő M1-gyertya ZÁRÁSA (nyitó + 1 M1 = a következő bar
nyitása), `YYYY.MM.DD HH:MM:SS` formátumban (MT5 StringToTime). FONTOS: a
Trading-with-Erik M1-BELÉPŐket ad (a Trading-with-ai M15-öt adott) → az EA-t is
M1-en kell futtatni (a BacktestReplayer.mq5 áthozott verziója M1-re állított).
"""

from __future__ import annotations

import csv
from pathlib import Path

import pandas as pd

MT5_HEADER = ["event", "datetime", "symbol", "direction", "price", "sl", "tp",
              "lot", "comment", "be_trigger", "trail_trigger", "trail_dist_p"]
BAR_MINUTES = 1     # Trading-with-Erik M1-belépő → az OPEN a belépő + 1 M1 (bar-záró)


def _fmt(dt) -> str:
    return pd.Timestamp(dt).strftime("%Y.%m.%d %H:%M:%S")


def events_from_result(result, symbol: str, params: dict, pair_cfg: dict,
                       comment: str = "wpr_sma") -> list[list]:
    """A BacktestResult trade-jeiből MT5-események (időrendben rendezve).
    OPEN: a belépő + a KEZDETI SL/TP + a BE/trail triggerek (az EA ebből kezel).
    CLOSE: a záró (vizualizációhoz; az EA végrehajtásnál figyelmen kívül hagyja)."""
    pip        = float(pair_cfg.get("pip_size", 0.0001))
    be_pct     = float(params.get("breakeven_pct", 0.5))
    trail_act  = float(params.get("trail_activation_pips", 8))
    trail_dist = float(params.get("trail_distance_pips", 6))

    events: list[tuple] = []   # (sort_ts, row)
    for t in getattr(result, "trades", []):
        sign    = 1 if t.direction == "BUY" else -1
        init_sl = round(t.open_price - sign * t.sl_pips * pip, 5)   # a nyitáskori (kezdeti) SL
        be_trig = round(t.open_price + sign * (t.tp - t.open_price) * be_pct, 5) if be_pct > 0 else 0.0
        tr_trig = round(t.open_price + sign * trail_act * pip, 5) if trail_act > 0 else 0.0
        o_ts    = pd.Timestamp(t.open_time) + pd.Timedelta(minutes=BAR_MINUTES)
        events.append((o_ts, [
            "OPEN", _fmt(o_ts), symbol, t.direction,
            round(t.open_price, 5), init_sl, round(t.tp, 5), t.lot, comment,
            be_trig, tr_trig, round(trail_dist, 5),
        ]))
        if getattr(t, "close_time", None) is not None and t.close_price is not None:
            c_ts = pd.Timestamp(t.close_time)
            events.append((c_ts, [
                "CLOSE", _fmt(c_ts), symbol, t.direction,
                round(t.close_price, 5), 0, 0, t.lot, t.status, 0, 0, 0,
            ]))
    events.sort(key=lambda e: e[0])     # időrend (az EA sorrendben dolgozza fel az OPEN-eket)
    return [row for _, row in events]


def export_mt5_csv(result, symbol: str, params: dict, pair_cfg: dict,
                   out_dir, comment: str = "wpr_sma") -> Path | None:
    """A CSV kiírása `mt5_backtest_<symbol>_<ts>.csv` néven. A fájlt az MT5
    `Common\\Files\\` mappájába kell másolni (lásd az EA `ide_kell_helyezni.txt`
    útmutatóját). Visszaad: a fájl útvonala, vagy None, ha nincs esemény."""
    rows = events_from_result(result, symbol, params, pair_cfg, comment)
    if not rows:
        return None
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = pd.Timestamp.now().strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"mt5_backtest_{symbol}_{ts}.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(MT5_HEADER)
        w.writerows(rows)
    return path
