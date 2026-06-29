"""
Backtestelő motor.

Működés:
  - M15 + M1 parquet fájlok beolvasása
  - Indikátorok számítása (SMA, WPR M15, WPR M1, ATR M15)
  - M1 szintű szimuláció: jelzés → pozíció nyitás → SL/TP/breakeven/trailing zárás
  - Slot menedzsment (portfólió szintű kockázat)

Futtatás: python trading/backtest.py
"""

import csv
import json
import logging
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import numpy as np

RESULTS_DIR = Path(__file__).resolve().parents[1] / "data" / "backtest_results"

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.indicator_engine import compute_indicators
from core.signal_detector import PairState, check_m15_signal, check_m1_entry
from core.risk_manager import calc_sl_tp_pips, calc_lot, calc_effective_slots

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Adatstruktúrák
# ---------------------------------------------------------------------------

@dataclass
class Trade:
    symbol: str
    direction: str          # "BUY" | "SELL"
    open_time: pd.Timestamp
    open_price: float
    sl: float
    tp: float
    lot: float
    pip_size: float
    pv1_usd: float
    sl_pips: float
    close_time: Optional[pd.Timestamp] = None
    close_price: Optional[float] = None
    pnl_usd: float = 0.0
    pnl_pips: float = 0.0
    status: str = "open"    # "open" | "tp" | "sl" | "be_trail"
    risk_free: bool = False  # SL átment breakeven-re
    entry_balance: float = 0.0
    risk_usd: float = 0.0
    risk_pct: float = 0.0


@dataclass
class BacktestResult:
    symbol: str
    trades: list = field(default_factory=list)
    balance_curve: list = field(default_factory=list)

    @property
    def closed(self):
        return [t for t in self.trades if t.status != "open"]

    def summary(self, initial_balance: float) -> dict:
        closed = self.closed
        if not closed:
            return {"symbol": self.symbol, "trades": 0}

        pnl_list = [t.pnl_usd for t in closed]
        wins   = [p for p in pnl_list if p > 0]
        losses = [p for p in pnl_list if p <= 0]
        tps    = [t for t in closed if t.status == "tp"]
        sls    = [t for t in closed if t.status == "sl"]
        be_tr  = [t for t in closed if t.status == "be_trail"]

        balance = initial_balance
        peak    = balance
        max_dd  = 0.0
        for p in pnl_list:
            balance += p
            if balance > peak:
                peak = balance
            dd = (peak - balance) / peak
            if dd > max_dd:
                max_dd = dd

        return {
            "symbol":        self.symbol,
            "trades":        len(closed),
            "win_rate":      len(wins) / len(closed) if closed else 0,
            "total_pnl":     sum(pnl_list),
            "avg_win":       sum(wins) / len(wins) if wins else 0,
            "avg_loss":      sum(losses) / len(losses) if losses else 0,
            "max_drawdown":  max_dd,
            "profit_factor": abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else float("inf"),
            "tp_count":      len(tps),
            "sl_count":      len(sls),
            "be_trail_count": len(be_tr),
            "final_balance": initial_balance + sum(pnl_list),
        }


# ---------------------------------------------------------------------------
# Segédfüggvények
# ---------------------------------------------------------------------------

def load_data(symbol: str) -> tuple[Optional[pd.DataFrame], Optional[pd.DataFrame]]:
    m15_path = ROOT / "data" / "m15" / f"{symbol}.parquet"
    m1_path  = ROOT / "data" / "m1"  / f"{symbol}.parquet"

    if not m15_path.exists() or not m1_path.exists():
        log.warning("%s — hiányzó adat fájl, kihagyva.", symbol)
        return None, None

    df_m15 = pd.read_parquet(m15_path)
    df_m1  = pd.read_parquet(m1_path)
    return df_m15, df_m1


def pip_to_price(pips: float, pip_size: float) -> float:
    return pips * pip_size


def calc_pnl(trade: Trade, close_price: float) -> float:
    diff = close_price - trade.open_price
    if trade.direction == "SELL":
        diff = -diff
    pips = diff / trade.pip_size
    return pips * trade.lot * trade.pv1_usd


# ---------------------------------------------------------------------------
# Fő szimuláció egy párra
# ---------------------------------------------------------------------------

def run_pair(
    symbol: str,
    df_m15: pd.DataFrame,
    df_m1: pd.DataFrame,
    params: dict,
    pair_cfg: dict,
    trading_cfg: dict,
    initial_balance: float,
    test_start: Optional[str] = None,
) -> BacktestResult:
    result = BacktestResult(symbol=symbol)

    # Indikátorok számítása
    m15, m1 = compute_indicators(df_m15, df_m1, params)

    # Warmup sor — az első érvényes indikátor sor
    warmup = max(params["sma_period"], params["wpr_m15_period"], params["atr_period"])
    m15 = m15.iloc[warmup:].copy()

    warmup_m1 = params["wpr_m1_period"]
    m1 = m1.iloc[warmup_m1:].copy()

    # Test/train szétválasztás
    if test_start:
        ts = pd.Timestamp(test_start)
        # Ha az index UTC-aware, a Timestamp-ot is UTC-ra kell állítani
        if m15.index.tzinfo is not None:
            ts = ts.tz_localize("UTC")
        m15 = m15[m15.index >= ts]
        m1  = m1[m1.index >= ts]

    # Session szűrő: params felülírja pair_cfg-t ha van (Optuna optimalizálja)
    sess_start = int(params.get("trade_hour_start", pair_cfg.get("sess_start", 0)))
    sess_end   = int(params.get("trade_hour_end",   pair_cfg.get("sess_end", 24)))
    skip_monday_hours = int(params.get("skip_monday_hours", 0))
    skip_friday_hour  = int(params.get("skip_friday_hour", 24))

    # ATR minőség szűrő (0 = kikapcs)
    atr_min_pct = float(params.get("atr_min_pct", 0.0))
    atr_max_pct = float(params.get("atr_max_pct", 0.0))
    avg_atr = float(m15["atr"].mean()) if "atr" in m15.columns and len(m15) > 0 else 0.0

    pip_size = pair_cfg["pip_size"]
    pv1_usd  = pair_cfg["pv1_usd"]

    state = PairState(symbol=symbol)
    open_trades: list[Trade] = []
    balance = initial_balance
    daily_pnl: dict[str, float] = {}  # dátum → napi P&L

    # M15 gyertyák indexe gyors kereséshez
    m15_times = m15.index.to_list()
    m15_ptr = 0  # melyik M15 gyertya az aktuális

    prev_m1_wpr = None

    for m1_time, m1_row in m1.iterrows():
        # Session szűrő
        hour = m1_time.hour
        if not (sess_start <= hour < sess_end):
            prev_m1_wpr = m1_row["wpr"]
            continue

        # Hétfő reggeli gap szűrő
        dow = m1_time.weekday()
        if dow == 0 and hour < skip_monday_hours:
            prev_m1_wpr = m1_row["wpr"]
            continue

        # Péntek délután szűrő
        if dow == 4 and hour >= skip_friday_hour:
            prev_m1_wpr = m1_row["wpr"]
            continue

        # Napi veszteség limit ellenőrzés
        day_key = str(m1_time.date())
        daily_loss = daily_pnl.get(day_key, 0.0)
        daily_limit = balance * trading_cfg["daily_loss_limit_pct"]
        if daily_loss <= -daily_limit:
            prev_m1_wpr = m1_row["wpr"]
            continue

        # M15 állapot frissítése ha új M15 gyertya zárult
        while m15_ptr < len(m15_times) - 1 and m15_times[m15_ptr + 1] <= m1_time:
            m15_ptr += 1

        if m15_ptr < len(m15_times):
            m15_row = m15.iloc[m15_ptr]

            # ATR minőség szűrő — csendes/kaotikus piac kizárása
            if avg_atr > 0 and "atr" in m15_row.index:
                cur_atr = m15_row["atr"]
                if not pd.isna(cur_atr):
                    if atr_min_pct > 0 and cur_atr < avg_atr * atr_min_pct:
                        prev_m1_wpr = m1_row["wpr"]
                        continue
                    if atr_max_pct > 0 and cur_atr > avg_atr * atr_max_pct:
                        prev_m1_wpr = m1_row["wpr"]
                        continue

            if not (pd.isna(m15_row["sma"]) or pd.isna(m15_row["wpr"]) or pd.isna(m15_row["atr"])):
                state = check_m15_signal(
                    state,
                    close=m15_row["close"],
                    sma=m15_row["sma"],
                    wpr_m15=m15_row["wpr"],
                    params=params,
                )

        # Nyitott pozíciók kezelése (SL/TP/breakeven/trailing)
        spread_pips = pair_cfg.get("backtest_spread_pips", 1.5)
        spread = pip_to_price(spread_pips, pip_size)

        for trade in list(open_trades):
            # BUY esetén az ár amit kapunk eladáskor = bid = close; vételkor = ask = close + spread
            bid = m1_row["low"]   # legrosszabb ár SL-hez (BUY SL ütés)
            ask = m1_row["high"]  # legrosszabb ár SL-hez (SELL SL ütés)

            closed = False

            if trade.direction == "BUY":
                # TP ellenőrzés
                if m1_row["high"] >= trade.tp:
                    trade.close_price = trade.tp
                    trade.close_time  = m1_time
                    trade.pnl_usd     = calc_pnl(trade, trade.tp)
                    trade.status      = "tp"
                    closed = True
                # SL ellenőrzés
                elif m1_row["low"] <= trade.sl:
                    trade.close_price = trade.sl
                    trade.close_time  = m1_time
                    trade.pnl_usd     = calc_pnl(trade, trade.sl)
                    trade.status      = "sl"
                    closed = True
                else:
                    # Breakeven
                    be_pct = params.get("breakeven_pct", 0.5)
                    if be_pct > 0 and not trade.risk_free:
                        be_trigger_price = trade.open_price + (trade.tp - trade.open_price) * be_pct
                        if m1_row["high"] >= be_trigger_price:
                            trade.sl = trade.open_price
                            trade.risk_free = True
                    # Trailing stop
                    trail_act = params.get("trail_activation_pips", 8)
                    trail_dist = params.get("trail_distance_pips", 6)
                    if trade.risk_free:
                        trail_trigger = trade.open_price + pip_to_price(trail_act, pip_size)
                        if m1_row["high"] >= trail_trigger:
                            new_sl = m1_row["high"] - pip_to_price(trail_dist, pip_size)
                            if new_sl > trade.sl:
                                trade.sl = new_sl

            elif trade.direction == "SELL":
                if m1_row["low"] <= trade.tp:
                    trade.close_price = trade.tp
                    trade.close_time  = m1_time
                    trade.pnl_usd     = calc_pnl(trade, trade.tp)
                    trade.status      = "tp"
                    closed = True
                elif m1_row["high"] >= trade.sl:
                    trade.close_price = trade.sl
                    trade.close_time  = m1_time
                    trade.pnl_usd     = calc_pnl(trade, trade.sl)
                    trade.status      = "sl"
                    closed = True
                else:
                    be_pct = params.get("breakeven_pct", 0.5)
                    if be_pct > 0 and not trade.risk_free:
                        be_trigger_price = trade.open_price - (trade.open_price - trade.tp) * be_pct
                        if m1_row["low"] <= be_trigger_price:
                            trade.sl = trade.open_price
                            trade.risk_free = True
                    trail_act = params.get("trail_activation_pips", 8)
                    trail_dist = params.get("trail_distance_pips", 6)
                    if trade.risk_free:
                        trail_trigger = trade.open_price - pip_to_price(trail_act, pip_size)
                        if m1_row["low"] <= trail_trigger:
                            new_sl = m1_row["low"] + pip_to_price(trail_dist, pip_size)
                            if new_sl < trade.sl:
                                trade.sl = new_sl

            if closed:
                # pnl_pips számítás
                if trade.direction == "BUY":
                    trade.pnl_pips = (trade.close_price - trade.open_price) / trade.pip_size - spread_pips
                else:
                    trade.pnl_pips = (trade.open_price - trade.close_price) / trade.pip_size - spread_pips
                open_trades.remove(trade)
                result.trades.append(trade)
                balance += trade.pnl_usd
                daily_pnl[day_key] = daily_pnl.get(day_key, 0.0) + trade.pnl_usd
                result.balance_curve.append((m1_time, balance))

        # Slot számítás
        occupied = sum(1 for t in open_trades if not t.risk_free)
        free_slots = trading_cfg["max_open_slots"] - occupied

        # M1 belépési jelzés ellenőrzés
        if prev_m1_wpr is not None and free_slots > 0 and not pd.isna(m1_row["wpr"]):
            signal = check_m1_entry(state, prev_m1_wpr, m1_row["wpr"], params)

            if signal != "NONE" and m15_ptr < len(m15_times):
                m15_row = m15.iloc[m15_ptr]
                atr_val = m15_row.get("atr", 0)

                if atr_val and atr_val > 0:
                    sl_pips, tp_pips = calc_sl_tp_pips(
                        atr_val, {**params, "pip_size": pip_size}
                    )
                    eff_slots = calc_effective_slots(balance, sl_pips, pair_cfg, trading_cfg)
                    lot = calc_lot(balance, sl_pips, pair_cfg, trading_cfg, eff_slots)

                    open_price = m1_row["close"]
                    if signal == "BUY":
                        open_price += pip_to_price(spread_pips, pip_size)
                        sl_price = open_price - pip_to_price(sl_pips, pip_size)
                        tp_price = open_price + pip_to_price(tp_pips, pip_size)
                    else:  # SELL
                        sl_price = open_price + pip_to_price(sl_pips, pip_size)
                        tp_price = open_price - pip_to_price(tp_pips, pip_size)

                    risk_usd = lot * sl_pips * pv1_usd
                    trade = Trade(
                        symbol=symbol,
                        direction=signal,
                        open_time=m1_time,
                        open_price=open_price,
                        sl=sl_price,
                        tp=tp_price,
                        lot=lot,
                        pip_size=pip_size,
                        pv1_usd=pv1_usd,
                        sl_pips=sl_pips,
                        entry_balance=balance,
                        risk_usd=risk_usd,
                        risk_pct=risk_usd / balance * 100 if balance > 0 else 0,
                    )
                    open_trades.append(trade)

        prev_m1_wpr = m1_row["wpr"]

    # Nyitva maradt pozíciók hozzáadása (nincs zárva)
    for trade in open_trades:
        result.trades.append(trade)

    return result


# ---------------------------------------------------------------------------
# Futtatás: összes aktív pár
# ---------------------------------------------------------------------------

def run_backtest(cfg: dict, params: Optional[dict] = None, test_mode: bool = False,
                 save_results: bool = True) -> list[dict]:
    initial_balance = cfg.get("ml", {}).get("starting_balance_eur", 1000.0)
    trading_cfg     = cfg["trading"]
    test_start      = cfg.get("optimizer", {}).get("test_start_date") if test_mode else None

    if params is None:
        params = {**cfg["indicators"], **cfg["sltp"], **cfg["position_mgmt"]}

    pairs = {s: p for s, p in cfg["pairs"].items() if isinstance(p, dict) and p.get("enabled", False)}
    summaries  = []
    all_trades: list[Trade] = []

    for symbol, pair_cfg in pairs.items():
        df_m15, df_m1 = load_data(symbol)
        if df_m15 is None:
            continue

        result = run_pair(
            symbol, df_m15, df_m1,
            params, pair_cfg, trading_cfg,
            initial_balance, test_start,
        )
        summary = result.summary(initial_balance)
        summaries.append(summary)
        all_trades.extend(result.closed)
        log.info(
            "%s | Kötések: %d | Win: %.0f%% | P&L: %.2f$ | MaxDD: %.1f%%",
            symbol,
            summary.get("trades", 0),
            summary.get("win_rate", 0) * 100,
            summary.get("total_pnl", 0),
            summary.get("max_drawdown", 0) * 100,
        )

    if save_results and all_trades:
        _save_backtest_results(all_trades, summaries, initial_balance, test_start)

    return summaries


def _save_backtest_results(trades: list, summaries: list[dict],
                           initial_balance: float, test_start: Optional[str]):
    """CSV + TXT kimenet mentése a data/backtest_results/ könyvtárba."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts_str = datetime.now().strftime("%Y%m%d_%H%M")

    # ── 1) trades CSV ────────────────────────────────────────────────────────
    trades_csv = RESULTS_DIR / f"trades_{ts_str}.csv"
    with open(trades_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "direction", "open_time", "close_time", "status",
                    "open_price", "close_price", "sl", "tp", "lot",
                    "sl_pips", "risk_usd", "risk_pct", "pnl_pips", "pnl_usd"])
        for t in sorted(trades, key=lambda x: x.open_time):
            w.writerow([
                t.symbol, t.direction,
                t.open_time, t.close_time, t.status,
                round(t.open_price, 5), round(t.close_price or 0, 5),
                round(t.sl, 5), round(t.tp, 5), t.lot,
                round(t.sl_pips, 1),
                round(t.risk_usd, 2), round(t.risk_pct, 3),
                round(t.pnl_pips, 2), round(t.pnl_usd, 2),
            ])

    # ── 2) daily CSV ─────────────────────────────────────────────────────────
    daily_pnl: dict = defaultdict(float)
    for t in trades:
        if t.close_time is not None:
            daily_pnl[t.close_time.date()] += t.pnl_usd

    daily_csv = RESULTS_DIR / f"daily_{ts_str}.csv"
    running = initial_balance
    with open(daily_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "pnl_usd", "balance", "ret_pct"])
        for date in sorted(daily_pnl):
            pv = daily_pnl[date]
            running += pv
            pct = pv / (running - pv) * 100 if (running - pv) > 0 else 0
            w.writerow([date, round(pv, 2), round(running, 2), round(pct, 4)])

    # ── 3) summary TXT ───────────────────────────────────────────────────────
    total_pnl    = sum(t.pnl_usd for t in trades)
    n_trades     = len(trades)
    n_wins       = sum(1 for t in trades if t.pnl_usd > 0)
    n_loss       = sum(1 for t in trades if t.pnl_usd <= 0)
    n_tp         = sum(1 for t in trades if t.status == "tp")
    n_sl         = sum(1 for t in trades if t.status == "sl")
    n_be         = sum(1 for t in trades if t.status == "be_trail")
    avg_w        = sum(t.pnl_usd for t in trades if t.pnl_usd > 0) / max(n_wins, 1)
    avg_l        = sum(t.pnl_usd for t in trades if t.pnl_usd < 0) / max(n_loss, 1)
    win_sum      = sum(t.pnl_usd for t in trades if t.pnl_usd > 0)
    loss_sum     = sum(t.pnl_usd for t in trades if t.pnl_usd < 0)
    pf           = abs(win_sum / loss_sum) if loss_sum != 0 else float("inf")
    final_bal    = initial_balance + total_pnl
    ret_pct      = total_pnl / initial_balance * 100

    # max drawdown portfólió szinten
    eq = initial_balance
    peak = eq; mdd = 0.0
    for t in sorted(trades, key=lambda x: x.close_time or x.open_time):
        eq += t.pnl_usd
        if eq > peak: peak = eq
        dd = (peak - eq) / peak * 100
        if dd > mdd: mdd = dd

    # havi bontás
    monthly_pnl: dict = defaultdict(float)
    monthly_trades: dict = defaultdict(int)
    for t in trades:
        if t.close_time:
            m = t.close_time.strftime("%Y-%m")
            monthly_pnl[m]    += t.pnl_usd
            monthly_trades[m] += 1

    # páronkénti összesítő
    by_sym: dict = defaultdict(list)
    for t in trades:
        by_sym[t.symbol].append(t)

    summary_txt = RESULTS_DIR / f"summary_{ts_str}.txt"
    with open(summary_txt, "w", encoding="utf-8") as f:
        f.write(f"BACKTEST ÖSSZESÍTŐ  —  {datetime.now():%Y-%m-%d %H:%M}\n")
        period = f"{min(daily_pnl):%Y-%m-%d} → {max(daily_pnl):%Y-%m-%d}" if daily_pnl else "—"
        mode   = f"TEST (out-of-sample, {test_start}-tól)" if test_start else "TELJES historikus"
        f.write(f"Mód       : {mode}\n")
        f.write(f"Időszak   : {period}  ({len(daily_pnl)} kereskedési nap)\n")
        f.write(f"Párok     : {len(by_sym)} aktív\n")
        f.write("=" * 65 + "\n")
        f.write(f"Trade-ek  : {n_trades}  ({n_trades/max(len(daily_pnl),1):.1f}/nap)\n")
        f.write(f"Exit típus: TP={n_tp}  SL={n_sl}  BE/Trail={n_be}\n")
        f.write(f"Nyerő     : {n_wins} ({n_wins/max(n_trades,1):.0%})  |  Vesztes: {n_loss} ({n_loss/max(n_trades,1):.0%})\n")
        f.write(f"Átl nyerő : ${avg_w:+.2f}   Átl vesztes: ${avg_l:+.2f}   Arány: {abs(avg_w/avg_l) if avg_l else 0:.2f}x\n")
        f.write(f"Profit F. : {pf:.2f}\n")
        f.write(f"Total P&L : ${total_pnl:+.2f}  ({ret_pct:+.1f}%)\n")
        f.write(f"Kezdő     : ${initial_balance:.0f}   Záró: ${final_bal:.0f}\n")
        f.write(f"Max DD    : {mdd:.2f}%\n")
        dp = sum(1 for v in daily_pnl.values() if v >= initial_balance * 0.01)
        f.write(f"1% napok  : {dp}/{len(daily_pnl)} ({dp/max(len(daily_pnl),1):.0%})\n")
        f.write("\nPÁRONKÉNT:\n")
        f.write(f"  {'Pár':<10} {'Trade':>6} {'Nyerő%':>7} {'P&L$':>9} {'MaxDD%':>8}\n")
        for sym, tt in sorted(by_sym.items(), key=lambda x: -sum(t.pnl_usd for t in x[1])):
            nw  = sum(1 for t in tt if t.pnl_usd > 0)
            ps  = sum(t.pnl_usd for t in tt)
            s   = next((x for x in summaries if x.get("symbol") == sym), {})
            dd  = s.get("max_drawdown", 0) * 100
            f.write(f"  {sym:<10} {len(tt):6d} {nw/len(tt):7.0%} {ps:+9.2f} {dd:8.1f}%\n")
        f.write("\nHAVI BONTÁS:\n")
        f.write(f"  {'Hónap':<10} {'Trade':>6} {'P&L$':>9} {'Ret%':>7}  chart\n")
        running2 = initial_balance
        for m in sorted(monthly_pnl):
            pv  = monthly_pnl[m]
            pct = pv / running2 * 100 if running2 > 0 else 0
            running2 += pv
            bar = "+" * min(int(abs(pct) * 3), 20) if pv >= 0 else "-" * min(int(abs(pct) * 3), 20)
            f.write(f"  {m:<10} {monthly_trades[m]:6d} {pv:+9.2f} {pct:+7.2f}%  {bar}\n")
        f.write("\nNAPI BONTÁS:\n")
        f.write(f"  {'Dátum':<12} {'P&L$':>9} {'Egyenleg':>10} {'Ret%':>7}  chart\n")
        running3 = initial_balance
        for date in sorted(daily_pnl):
            pv  = daily_pnl[date]
            prev = running3
            running3 += pv
            pct = pv / prev * 100 if prev > 0 else 0
            bar = "+" * min(int(abs(pct) * 8), 22) if pv >= 0 else "-" * min(int(abs(pct) * 8), 22)
            tag = " TARGET" if pct >= 1.0 else (" STOP" if pct < -1.0 else "")
            f.write(f"  {str(date):<12} {pv:+9.2f} ${running3:9.0f} {pct:+7.2f}%  {bar}{tag}\n")

    log.info("=" * 65)
    log.info("ÖSSZESÍTETT | Kötések: %d | Win: %.0f%% | P&L: %.2f$ | MaxDD: %.1f%% | PF: %.2f",
             n_trades, n_wins / max(n_trades, 1) * 100, total_pnl, mdd, pf)
    log.info("Eredmények mentve -> %s", RESULTS_DIR)
    log.info("  trades_%s.csv", ts_str)
    log.info("  daily_%s.csv",  ts_str)
    log.info("  summary_%s.txt", ts_str)


# ---------------------------------------------------------------------------
# Portfolio Backtest — közös tőke, közös slot rendszer, kronológiai szimuláció
# ---------------------------------------------------------------------------

PARAMS_DIR = ROOT / "data" / "optimized_params"


def _advance_m15_state(pd_info: dict, m1_time: pd.Timestamp) -> None:
    """M15 pointer előrehaladása és állapot frissítése (egy pár adatain)."""
    m15_times = pd_info["m15_times"]
    m15_df    = pd_info["m15"]
    params    = pd_info["params"]

    while (pd_info["m15_ptr"] < len(m15_times) - 1 and
           m15_times[pd_info["m15_ptr"] + 1] <= m1_time):
        pd_info["m15_ptr"] += 1

    ptr = pd_info["m15_ptr"]
    if ptr < len(m15_times):
        row = m15_df.iloc[ptr]
        sma = row.get("sma", float("nan"))
        wpr = row.get("wpr", float("nan"))
        atr = row.get("atr", float("nan"))
        if not (pd.isna(sma) or pd.isna(wpr) or pd.isna(atr)):
            pd_info["state"] = check_m15_signal(
                pd_info["state"],
                close=float(row["close"]),
                sma=float(sma),
                wpr_m15=float(wpr),
                params=params,
            )


def run_portfolio_backtest(
    cfg: dict,
    symbols: list,
    date_from: str,
    date_to: str,
    initial_balance: Optional[float] = None,
    progress_callback=None,   # fn(date_str, balance, n_open, n_closed, pct_done)
    stop_flag=None,           # threading.Event — ha set(), leállítja
) -> dict:
    """
    Portfólió szintű backtest: az összes szimbólum közös tőkén,
    kronológiai M1 szimulációval fut.

    Optimalizált params betöltése: data/optimized_params/<SYMBOL>.json
    """
    if initial_balance is None:
        initial_balance = float(cfg.get("ml", {}).get("starting_balance_eur", 1000.0))
    trading_cfg = cfg["trading"]
    spread_default = 1.5

    # ── Per-pár optimalizált paraméterek betöltése ────────────────────────
    pair_params: dict = {}
    for sym in symbols:
        f = PARAMS_DIR / f"{sym}.json"
        if f.exists():
            with open(f, encoding="utf-8") as fh:
                data = json.load(fh)
            pair_params[sym] = data.get("params")
        else:
            log.warning("Portfolio BT: %s — nincs optimalizált params, kihagyva.", sym)

    if not pair_params:
        return {"error": "Nincs optimalizált paraméter egyetlen szimbólumhoz sem.",
                "trades": [], "daily_pnl": {}, "final_balance": initial_balance}

    # ── Adat betöltés és indikátor számítás ──────────────────────────────
    pair_data: dict = {}
    ts_from = pd.Timestamp(date_from)
    ts_to   = pd.Timestamp(date_to)
    # UTC-aware indexhez UTC-aware timestamp kell
    _sample_df = next(iter(pair_data.values()), None) if False else None  # placeholder
    _tz_needed = True  # mindig UTC-ra normalizálunk biztonságból
    if ts_from.tzinfo is None:
        ts_from = ts_from.tz_localize("UTC")
    if ts_to.tzinfo is None:
        ts_to = ts_to.tz_localize("UTC")

    for sym, params in pair_params.items():
        if sym not in cfg["pairs"] or not isinstance(cfg["pairs"][sym], dict):
            continue
        df_m15, df_m1 = load_data(sym)
        if df_m15 is None:
            log.warning("Portfolio BT: %s — nincs adat, kihagyva.", sym)
            continue

        m15, m1 = compute_indicators(df_m15, df_m1, params)

        warmup_m15 = max(params["sma_period"], params["wpr_m15_period"], params["atr_period"])
        warmup_m1  = params["wpr_m1_period"]
        m15 = m15.iloc[warmup_m15:].copy()
        m1  = m1.iloc[warmup_m1:].copy()

        # Dátum szűrés
        m15 = m15[(m15.index >= ts_from) & (m15.index <= ts_to)]
        m1  = m1[(m1.index  >= ts_from) & (m1.index  <= ts_to)]

        if len(m1) < 100:
            log.warning("Portfolio BT: %s — túl kevés adat a megadott időszakban.", sym)
            continue

        pair_data[sym] = {
            "m15":       m15,
            "m1":        m1,
            "m15_times": m15.index.tolist(),
            "m15_ptr":   0,
            "params":    params,
            "pair_cfg":  cfg["pairs"][sym],
            "state":     PairState(symbol=sym),
            "prev_wpr":  None,
        }
        log.info("Portfolio BT: %s betöltve — M15=%d M1=%d bar", sym, len(m15), len(m1))

    if not pair_data:
        return {"error": "Nincs elegendő adat a megadott időszakban.",
                "trades": [], "daily_pnl": {}, "final_balance": initial_balance}

    # ── Egységes M1 idővonal ──────────────────────────────────────────────
    all_times: set = set()
    for info in pair_data.values():
        all_times.update(info["m1"].index.tolist())
    all_times_sorted = sorted(all_times)
    n_total = len(all_times_sorted)
    log.info("Portfolio BT: %d pár | %d M1 bar | tőke: $%.0f",
             len(pair_data), n_total, initial_balance)

    # ── Szimuláció állapot ────────────────────────────────────────────────
    balance        = initial_balance
    open_trades:   dict  = {}   # sym → Trade (max 1 pozíció/szimbólum)
    closed_trades: list  = []
    daily_pnl:     dict  = {}
    equity_curve:  list  = []   # (date_str, balance) pontok a görbe rajzához
    max_slots      = trading_cfg["max_open_slots"]

    last_eq_date = ""

    for bar_idx, m1_time in enumerate(all_times_sorted):
        # Stop flag
        if stop_flag and stop_flag.is_set():
            log.info("Portfolio BT: leállítva a felhasználó által.")
            break

        day_key  = str(m1_time.date())
        date_str = day_key

        # Progress callback (~200 alkalommal összesen)
        if bar_idx % max(1, n_total // 200) == 0 or bar_idx == n_total - 1:
            pct = (bar_idx + 1) / n_total * 100
            if progress_callback:
                progress_callback(date_str, balance,
                                  len(open_trades), len(closed_trades), pct)
            # Equity curve pont napváltáskor
            if date_str != last_eq_date:
                equity_curve.append((date_str, balance))
                last_eq_date = date_str

        # ── 1. M15 állapot frissítése minden párhoz ───────────────────────
        for info in pair_data.values():
            _advance_m15_state(info, m1_time)

        # ── 2. Nyitott pozíciók kezelése ──────────────────────────────────
        for sym in list(open_trades.keys()):
            trade  = open_trades[sym]
            info   = pair_data[sym]
            m1_df  = info["m1"]
            params = info["params"]

            if m1_time not in m1_df.index:
                continue
            row      = m1_df.loc[m1_time]
            pip_size = trade.pip_size
            sp       = info["pair_cfg"].get("backtest_spread_pips", spread_default)
            closed   = False

            if trade.direction == "BUY":
                if row["high"] >= trade.tp:
                    trade.close_price = trade.tp
                    trade.close_time  = m1_time
                    trade.pnl_usd     = calc_pnl(trade, trade.tp)
                    trade.pnl_pips    = (trade.tp - trade.open_price) / pip_size - sp
                    trade.status      = "tp";  closed = True
                elif row["low"] <= trade.sl:
                    trade.close_price = trade.sl
                    trade.close_time  = m1_time
                    trade.pnl_usd     = calc_pnl(trade, trade.sl)
                    trade.pnl_pips    = (trade.sl - trade.open_price) / pip_size - sp
                    trade.status      = "sl";  closed = True
                else:
                    be_pct = params.get("breakeven_pct", 0.5)
                    if be_pct > 0 and not trade.risk_free:
                        if row["high"] >= trade.open_price + (trade.tp - trade.open_price) * be_pct:
                            trade.sl = trade.open_price;  trade.risk_free = True
                    if trade.risk_free:
                        trig = trade.open_price + pip_to_price(params.get("trail_activation_pips", 8), pip_size)
                        if row["high"] >= trig:
                            cand = row["high"] - pip_to_price(params.get("trail_distance_pips", 6), pip_size)
                            if cand > trade.sl:
                                trade.sl = cand

            else:  # SELL
                if row["low"] <= trade.tp:
                    trade.close_price = trade.tp
                    trade.close_time  = m1_time
                    trade.pnl_usd     = calc_pnl(trade, trade.tp)
                    trade.pnl_pips    = (trade.open_price - trade.tp) / pip_size - sp
                    trade.status      = "tp";  closed = True
                elif row["high"] >= trade.sl:
                    trade.close_price = trade.sl
                    trade.close_time  = m1_time
                    trade.pnl_usd     = calc_pnl(trade, trade.sl)
                    trade.pnl_pips    = (trade.open_price - trade.sl) / pip_size - sp
                    trade.status      = "sl";  closed = True
                else:
                    be_pct = params.get("breakeven_pct", 0.5)
                    if be_pct > 0 and not trade.risk_free:
                        if row["low"] <= trade.open_price - (trade.open_price - trade.tp) * be_pct:
                            trade.sl = trade.open_price;  trade.risk_free = True
                    if trade.risk_free:
                        trig = trade.open_price - pip_to_price(params.get("trail_activation_pips", 8), pip_size)
                        if row["low"] <= trig:
                            cand = row["low"] + pip_to_price(params.get("trail_distance_pips", 6), pip_size)
                            if cand < trade.sl:
                                trade.sl = cand

            if closed:
                del open_trades[sym]
                closed_trades.append(trade)
                balance += trade.pnl_usd
                daily_pnl[day_key] = daily_pnl.get(day_key, 0.0) + trade.pnl_usd

        # ── 3. Napi limit ellenőrzés (portfólió szinten) ──────────────────
        daily_loss  = daily_pnl.get(day_key, 0.0)
        daily_limit = balance * trading_cfg["daily_loss_limit_pct"]
        if daily_loss <= -daily_limit:
            continue   # nap leállt, nem nyitunk új pozíciót

        # ── 4. Új belépések ellenőrzése ────────────────────────────────────
        occupied = sum(1 for t in open_trades.values() if not t.risk_free)

        for sym, info in pair_data.items():
            if sym in open_trades:
                continue
            if occupied >= max_slots:
                break

            m1_df    = info["m1"]
            params   = info["params"]
            pair_cfg = info["pair_cfg"]

            if m1_time not in m1_df.index:
                continue
            row  = m1_df.loc[m1_time]
            hour = m1_time.hour
            if not (pair_cfg.get("sess_start", 0) <= hour < pair_cfg.get("sess_end", 24)):
                info["prev_wpr"] = float(row["wpr"]) if not pd.isna(row.get("wpr")) else None
                continue

            curr_wpr = row.get("wpr")
            prev_wpr = info["prev_wpr"]

            if prev_wpr is not None and curr_wpr is not None and not pd.isna(curr_wpr):
                signal = check_m1_entry(info["state"], prev_wpr, float(curr_wpr), params)

                if signal != "NONE":
                    ptr = info["m15_ptr"]
                    m15_df = info["m15"]
                    if ptr < len(m15_df):
                        m15_row = m15_df.iloc[ptr]
                        atr_val = m15_row.get("atr", 0)
                        if atr_val and atr_val > 0:
                            pip_size = pair_cfg["pip_size"]
                            pv1_usd  = pair_cfg["pv1_usd"]
                            sp       = pair_cfg.get("backtest_spread_pips", spread_default)

                            sl_pips, tp_pips = calc_sl_tp_pips(
                                atr_val, {**params, "pip_size": pip_size}
                            )
                            eff_slots = calc_effective_slots(balance, sl_pips, pair_cfg, trading_cfg)
                            lot = calc_lot(balance, sl_pips, pair_cfg, trading_cfg, eff_slots)

                            open_price = float(row["close"])
                            if signal == "BUY":
                                open_price += pip_to_price(sp, pip_size)
                                sl_price = open_price - pip_to_price(sl_pips, pip_size)
                                tp_price = open_price + pip_to_price(tp_pips, pip_size)
                            else:
                                sl_price = open_price + pip_to_price(sl_pips, pip_size)
                                tp_price = open_price - pip_to_price(tp_pips, pip_size)

                            risk_usd = lot * sl_pips * pv1_usd
                            trade = Trade(
                                symbol=sym, direction=signal,
                                open_time=m1_time, open_price=open_price,
                                sl=sl_price, tp=tp_price, lot=lot,
                                pip_size=pip_size, pv1_usd=pv1_usd, sl_pips=sl_pips,
                                entry_balance=balance,
                                risk_usd=risk_usd,
                                risk_pct=risk_usd / balance * 100 if balance > 0 else 0,
                            )
                            open_trades[sym] = trade
                            occupied += 1

            info["prev_wpr"] = float(curr_wpr) if not pd.isna(curr_wpr) else None

    # ── Végeredmény ───────────────────────────────────────────────────────
    if progress_callback:
        progress_callback(last_eq_date or date_str, balance,
                          0, len(closed_trades), 100.0)
    equity_curve.append((last_eq_date, balance))

    # Per-pár összesítő
    by_sym: dict = defaultdict(list)
    for t in closed_trades:
        by_sym[t.symbol].append(t)

    per_pair: dict = {}
    for sym, tt in by_sym.items():
        r = BacktestResult(symbol=sym, trades=tt)
        per_pair[sym] = r.summary(initial_balance)

    log.info("Portfolio BT kész | Kötések: %d | P&L: $%.2f | Végegyenleg: $%.2f",
             len(closed_trades), balance - initial_balance, balance)

    return {
        "trades":          closed_trades,
        "daily_pnl":       daily_pnl,
        "final_balance":   balance,
        "initial_balance": initial_balance,
        "per_pair":        per_pair,
        "equity_curve":    equity_curve,
    }


if __name__ == "__main__":
    cfg_path = ROOT / "config.json"
    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)

    run_backtest(cfg, save_results=True)
