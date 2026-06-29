"""
Élő kereskedési motor.

Működés:
  - MT5 kapcsolódás
  - Optimalizált paraméterek betöltése (data/optimized_params/<SYMBOL>.json)
  - M15 + M1 gyertyák valós idejű figyelése minden LIVE állapotú párhoz
  - BUY/SELL jelzés → pozíció nyitás (lot, SL, TP számítással)
  - Pozíció menedzsment: breakeven, trailing stop, napi limit
  - Slot kezelés: kockázatmentes pozíció felszabadítja a slotot
  - Play/Stop vezérlés páronként (GUI-ból)

Futtatás: python trading/live_trader.py
"""

import json
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import MetaTrader5 as mt5
import pandas as pd
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import mt5_connector
from core.indicator_engine import compute_indicators
from core.signal_detector import PairState, check_m15_signal, check_m1_entry
from core.risk_manager import calc_sl_tp_pips, calc_lot, calc_effective_slots, SlotManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

PARAMS_DIR   = ROOT / "data" / "optimized_params"
TRADES_CSV   = ROOT / "trades.csv"


def load_pair_params(symbol: str) -> Optional[dict]:
    """Per-pár params betöltése: data/optimized_params/<SYMBOL>.json"""
    f = PARAMS_DIR / f"{symbol}.json"
    if not f.exists():
        return None
    with open(f, encoding="utf-8") as fh:
        data = json.load(fh)
    return data.get("params")


# ---------------------------------------------------------------------------
# Dashboard adatcsatorna (shared state a GUI-val)
# ---------------------------------------------------------------------------

@dataclass
class PairDashboardState:
    symbol:           str

    # ── Váz-szintű (stratégia-független) mezők ───────────────────────────
    bid:              Optional[float] = None   # aktuális eladási ár
    ask:              Optional[float] = None   # aktuális vételi ár
    digits:           int  = 5                 # árformázás tizedesjegyek
    prev_bid:         Optional[float] = None   # előző bid (tick-színezéshez)
    prev_ask:         Optional[float] = None   # előző ask
    day_open:         Optional[float] = None   # napi nyitóár (változás% alaphoz)
    change_pct:       Optional[float] = None   # napi változás %-ban
    position_pnl:     Optional[float] = None   # None = nincs nyitott pozíció
    risk_free:        bool = False
    daily_pnl:        float = 0.0
    enabled:          bool = True
    trained:          bool = False  # van-e optimalizált paraméter
    spread_pts:       int  = 0      # aktuális spread pontban (MT5 egység)
    max_spread_pts:   int  = 0      # megengedett max spread pontban
    timeframe_remaining: dict = field(default_factory=dict)  # {percek: hátralévő mp}

    # ── Stratégia-specifikus cellák: {oszlop_kulcs: (szöveg, szín-név)} ───
    strategy_cells:   dict = field(default_factory=dict)

    # ── Régi (stratégia-specifikus) mezők — Fázis 3-ban megszűnnek ───────
    # A live_trader még ezeket írja; a GUI már a strategy_cells-ből olvas.
    sma_direction:    str  = "—"
    wpr_m15:          float = 0.0
    m15_signal:       str  = "—"
    m15_remaining_s:  int  = 0
    wpr_m1:           float = 0.0
    m1_signal:        str  = "—"
    m1_remaining_s:   int  = 0


# Globális dashboard állapot — a GUI ebből olvas
dashboard: dict[str, PairDashboardState] = {}

# Globális Play/Stop vezérlés — a GUI írja, a run() olvassa
# Értékek: "LIVE" | "STOPPED" | "OPTIMIZING" | "QUEUED"
instrument_state: dict[str, str] = {}

# Optimizer státusz szöveg per pár (progress, "Várakozik...", "Kész ✓")
optimizer_status: dict[str, str] = {}


# ---------------------------------------------------------------------------
# MT5 segédfüggvények
# ---------------------------------------------------------------------------

def get_candles(symbol: str, timeframe, count: int) -> Optional[pd.DataFrame]:
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
    if rates is None or len(rates) == 0:
        return None
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df.set_index("time", inplace=True)
    df.rename(columns={"tick_volume": "volume"}, inplace=True)
    return df[["open", "high", "low", "close", "volume"]]


def open_position(
    symbol: str,
    direction: str,
    lot: float,
    sl: float,
    tp: float,
    magic: int,
    comment: str = "ErikBot",
) -> Optional[int]:
    order_type = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        log.error("%s — nem sikerült az ár lekérdezés.", symbol)
        return None

    price = tick.ask if direction == "BUY" else tick.bid

    request = {
        "action":    mt5.TRADE_ACTION_DEAL,
        "symbol":    symbol,
        "volume":    lot,
        "type":      order_type,
        "price":     price,
        "sl":        sl,
        "tp":        tp,
        "magic":     magic,
        "comment":   comment,
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        log.error("%s — pozíció nyitás hiba: %s", symbol,
                  result.comment if result else mt5.last_error())
        return None

    log.info("✅ %s %s | Lot: %.2f | SL: %.5f | TP: %.5f | Ticket: %d",
             symbol, direction, lot, sl, tp, result.order)
    return result.order


def modify_sl(ticket: int, new_sl: float) -> bool:
    position = mt5.positions_get(ticket=ticket)
    if not position:
        return False
    pos = position[0]
    request = {
        "action":   mt5.TRADE_ACTION_SLTP,
        "symbol":   pos.symbol,
        "sl":       new_sl,
        "tp":       pos.tp,
        "position": ticket,
    }
    result = mt5.order_send(request)
    return result is not None and result.retcode == mt5.TRADE_RETCODE_DONE


def get_open_positions(magic: int) -> list:
    positions = mt5.positions_get()
    if positions is None:
        return []
    return [p for p in positions if p.magic == magic]


def pip_to_price(pips: float, pip_size: float) -> float:
    return pips * pip_size


def seconds_to_candle_close(timeframe_minutes: int) -> int:
    now = datetime.now(timezone.utc)
    seconds_in_tf = timeframe_minutes * 60
    elapsed = (now.minute % timeframe_minutes) * 60 + now.second
    return seconds_in_tf - elapsed


def log_trade(row: dict):
    df = pd.DataFrame([row])
    if TRADES_CSV.exists():
        df.to_csv(TRADES_CSV, mode="a", header=False, index=False)
    else:
        df.to_csv(TRADES_CSV, index=False)


# ---------------------------------------------------------------------------
# Egy pár állapota futás közben
# ---------------------------------------------------------------------------

@dataclass
class LivePairState:
    symbol:      str
    pair_cfg:    dict
    params:      dict
    trading_cfg: dict
    magic:       int
    signal_state: PairState = field(default_factory=lambda: PairState(""))
    prev_m1_wpr:  Optional[float] = None
    last_m15_time: Optional[pd.Timestamp] = None
    daily_pnl:    float = 0.0
    daily_date:   str   = ""

    def __post_init__(self):
        self.signal_state = PairState(self.symbol)


# ---------------------------------------------------------------------------
# Fő kereskedési ciklus
# ---------------------------------------------------------------------------

def process_pair(state: LivePairState, slot_mgr: SlotManager, balance: float):
    symbol     = state.symbol
    pair_cfg   = state.pair_cfg
    params     = state.params
    trading_cfg = state.trading_cfg
    magic      = state.magic
    pip_size   = pair_cfg["pip_size"]
    pv1_usd    = pair_cfg["pv1_usd"]

    # Dashboard állapot inicializálás
    ds = dashboard.setdefault(symbol, PairDashboardState(symbol=symbol, trained=True))
    ds.m15_remaining_s = seconds_to_candle_close(15)
    ds.m1_remaining_s  = seconds_to_candle_close(1)

    # Session szűrő
    hour = datetime.now(timezone.utc).hour
    sess_start = pair_cfg.get("sess_start", 0)
    sess_end   = pair_cfg.get("sess_end", 24)
    if not (sess_start <= hour < sess_end):
        return

    # Napi reset
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.daily_date != today:
        state.daily_date = today
        state.daily_pnl  = 0.0

    # Napi limit
    daily_limit = balance * trading_cfg["daily_loss_limit_pct"]
    if state.daily_pnl <= -daily_limit:
        log.debug("%s — napi veszteség limit elérve, kihagyva.", symbol)
        return

    warmup = max(params["sma_period"], params["wpr_m15_period"], params["atr_period"]) + 5

    # --- M15 adatok ---
    df_m15 = get_candles(symbol, mt5.TIMEFRAME_M15, warmup)
    if df_m15 is None or len(df_m15) < warmup:
        return

    # --- M1 adatok ---
    warmup_m1 = params["wpr_m1_period"] + 5
    df_m1 = get_candles(symbol, mt5.TIMEFRAME_M1, warmup_m1)
    if df_m1 is None or len(df_m1) < warmup_m1:
        return

    # Indikátorok számítása
    m15, m1 = compute_indicators(df_m15, df_m1, params)

    # Utolsó zárt M15 gyertya (index -2: az utolsó zárt, -1: az aktuális nyitott)
    m15_closed = m15.iloc[-2]
    m15_time   = m15.index[-2]

    if pd.isna(m15_closed["sma"]) or pd.isna(m15_closed["wpr"]) or pd.isna(m15_closed["atr"]):
        return

    # --- Spread ellenőrzés ---
    sym_info = mt5.symbol_info(symbol)
    if sym_info:
        current_spread_pts = sym_info.spread  # MT5 pontban adja (pl. 12)
        atr_pts = int(m15_closed["atr"] / sym_info.point)
        ratio   = params.get("max_spread_atr_ratio", 0.20)
        max_spread_pts = max(1, int(atr_pts * ratio))
        ds.spread_pts     = current_spread_pts
        ds.max_spread_pts = max_spread_pts

    # M15 állapot frissítése ha új gyertya zárult
    if state.last_m15_time != m15_time:
        state.last_m15_time = m15_time
        state.signal_state = check_m15_signal(
            state.signal_state,
            close=m15_closed["close"],
            sma=m15_closed["sma"],
            wpr_m15=m15_closed["wpr"],
            params=params,
        )

    # Dashboard frissítés (M15)
    ds.sma_direction = state.signal_state.direction
    ds.wpr_m15 = round(m15_closed["wpr"], 1)
    if state.signal_state.m15_window_open:
        ds.m15_signal = f"{state.signal_state.direction}{'▲' if state.signal_state.direction == 'BUY' else '▼'}"
    else:
        ds.m15_signal = "—"

    # Utolsó zárt M1 gyertya
    m1_closed = m1.iloc[-2]
    m1_prev   = m1.iloc[-3] if len(m1) >= 3 else m1.iloc[-2]

    if pd.isna(m1_closed["wpr"]) or pd.isna(m1_prev["wpr"]):
        return

    ds.wpr_m1 = round(m1_closed["wpr"], 1)

    # --- Pozíció menedzsment ---
    open_positions = get_open_positions(magic)
    symbol_positions = [p for p in open_positions if p.symbol == symbol]

    for pos in symbol_positions:
        ticket = pos.ticket
        pnl    = pos.profit
        is_rf  = slot_mgr.is_risk_free(ticket)

        # Breakeven ellenőrzés
        be_pct = params.get("breakeven_pct", 0.5)
        if be_pct > 0 and not is_rf:
            if pos.type == mt5.ORDER_TYPE_BUY:
                tp_dist  = pos.tp - pos.price_open
                be_price = pos.price_open + tp_dist * be_pct
                if pos.price_current >= be_price:
                    if modify_sl(ticket, pos.price_open):
                        slot_mgr.set_risk_free(ticket)
                        log.info("✦ %s #%d — breakeven beállítva", symbol, ticket)
            else:
                tp_dist  = pos.price_open - pos.tp
                be_price = pos.price_open - tp_dist * be_pct
                if pos.price_current <= be_price:
                    if modify_sl(ticket, pos.price_open):
                        slot_mgr.set_risk_free(ticket)
                        log.info("✦ %s #%d — breakeven beállítva", symbol, ticket)

        # Trailing stop (csak kockázatmentes után)
        is_rf = slot_mgr.is_risk_free(ticket)
        if is_rf:
            trail_act  = params.get("trail_activation_pips", 8)
            trail_dist = params.get("trail_distance_pips", 6)
            if pos.type == mt5.ORDER_TYPE_BUY:
                trail_trigger = pos.price_open + pip_to_price(trail_act, pip_size)
                if pos.price_current >= trail_trigger:
                    new_sl = pos.price_current - pip_to_price(trail_dist, pip_size)
                    if new_sl > pos.sl:
                        modify_sl(ticket, round(new_sl, 5))
            else:
                trail_trigger = pos.price_open - pip_to_price(trail_act, pip_size)
                if pos.price_current <= trail_trigger:
                    new_sl = pos.price_current + pip_to_price(trail_dist, pip_size)
                    if new_sl < pos.sl:
                        modify_sl(ticket, round(new_sl, 5))

        # Dashboard P&L frissítés
        ds.position_pnl = pnl
        ds.risk_free     = slot_mgr.is_risk_free(ticket)

    # Lezárt pozíciók észlelése (eltűnt a listából)
    for ticket in slot_mgr.all_tickets():
        still_open = any(p.ticket == ticket for p in symbol_positions)
        if not still_open:
            # Lezárva — kereskedési napló
            history = mt5.history_deals_get(position=ticket)
            if history:
                last_deal = history[-1]
                pnl = last_deal.profit + last_deal.commission + last_deal.swap
                state.daily_pnl += pnl
                ds.daily_pnl     = state.daily_pnl
                ds.position_pnl  = None
                ds.risk_free      = False
                slot_mgr.remove(ticket)
                log.info("📋 %s #%d zárt | P&L: %.2f$", symbol, ticket, pnl)
                log_trade({
                    "time":    datetime.now(timezone.utc).isoformat(),
                    "symbol":  symbol,
                    "ticket":  ticket,
                    "pnl_usd": pnl,
                })

    # --- M1 belépési jelzés ---
    spread_ok = (ds.max_spread_pts == 0 or ds.spread_pts <= ds.max_spread_pts)
    if not spread_ok:
        log.debug("%s — spread túl nagy: %d pt > %d pt max, kihagyva.",
                  symbol, ds.spread_pts, ds.max_spread_pts)

    if state.prev_m1_wpr is not None and slot_mgr.can_open() and spread_ok:
        signal = check_m1_entry(state.signal_state, state.prev_m1_wpr, m1_closed["wpr"], params)

        if signal != "NONE":
            atr_val = m15_closed["atr"]
            sl_pips, tp_pips = calc_sl_tp_pips(atr_val, {**params, "pip_size": pip_size})
            eff_slots = calc_effective_slots(balance, sl_pips, pair_cfg, trading_cfg)
            lot = calc_lot(balance, sl_pips, pair_cfg, trading_cfg, eff_slots)

            tick = mt5.symbol_info_tick(symbol)
            if tick:
                if signal == "BUY":
                    open_price = tick.ask
                    sl_price   = round(open_price - pip_to_price(sl_pips, pip_size), 5)
                    tp_price   = round(open_price + pip_to_price(tp_pips, pip_size), 5)
                else:
                    open_price = tick.bid
                    sl_price   = round(open_price + pip_to_price(sl_pips, pip_size), 5)
                    tp_price   = round(open_price - pip_to_price(tp_pips, pip_size), 5)

                ticket = open_position(symbol, signal, lot, sl_price, tp_price, magic)
                if ticket:
                    slot_mgr.add(ticket)
                    ds.m1_signal = f"{signal}{'▲' if signal == 'BUY' else '▼'}"

    state.prev_m1_wpr = m1_closed["wpr"]


# ---------------------------------------------------------------------------
# Fő ciklus
# ---------------------------------------------------------------------------

def run(cfg: dict, slot_mgr: SlotManager):
    magic       = cfg["broker"]["magic"]
    trading_cfg = cfg["trading"]

    all_pairs = {s: p for s, p in cfg["pairs"].items() if isinstance(p, dict)}
    pair_states: dict[str, LivePairState] = {}

    # Dashboard + instrument_state inicializálás minden párhoz
    for symbol, pair_cfg in all_pairs.items():
        params = load_pair_params(symbol)
        trained = params is not None
        dashboard[symbol] = PairDashboardState(
            symbol=symbol,
            enabled=pair_cfg.get("enabled", False),
            trained=trained,
        )
        # Kezdeti állapot: ha enabled és trained → LIVE, egyébként STOPPED
        if pair_cfg.get("enabled", False) and trained:
            instrument_state[symbol] = "LIVE"
            pair_states[symbol] = LivePairState(
                symbol=symbol,
                pair_cfg=pair_cfg,
                params=params,
                trading_cfg=trading_cfg,
                magic=magic,
            )
            log.info("%s — LIVE (params betöltve)", symbol)
        else:
            instrument_state[symbol] = "STOPPED"
            if not trained:
                log.info("%s — STOPPED (nincs params, futtatsd az optimalizálást)", symbol)

    log.info("Élő kereskedés indul | %d LIVE pár", len(pair_states))

    while True:
        try:
            balance = mt5_connector.account_balance()

            for symbol, pair_cfg in all_pairs.items():
                state_now = instrument_state.get(symbol, "STOPPED")

                # Play → LIVE: új LivePairState létrehozása friss params-szal
                if state_now == "LIVE" and symbol not in pair_states:
                    params = load_pair_params(symbol)
                    if params:
                        pair_states[symbol] = LivePairState(
                            symbol=symbol,
                            pair_cfg=pair_cfg,
                            params=params,
                            trading_cfg=trading_cfg,
                            magic=magic,
                        )
                        log.info("%s — Play: LIVE indítva", symbol)
                    else:
                        instrument_state[symbol] = "STOPPED"
                        log.warning("%s — Play: nincs params, visszaállítva STOPPED-ra", symbol)

                # Stop → eltávolítás a pair_states-ből
                elif state_now == "STOPPED" and symbol in pair_states:
                    del pair_states[symbol]
                    log.info("%s — Stop: LIVE leállítva", symbol)

                # LIVE: feldolgozás
                if state_now == "LIVE" and symbol in pair_states:
                    process_pair(pair_states[symbol], slot_mgr, balance)

            time.sleep(10)

        except KeyboardInterrupt:
            log.info("Leállítás...")
            break
        except Exception as e:
            log.error("Hiba a fő ciklusban: %s", e, exc_info=True)
            time.sleep(30)


# ---------------------------------------------------------------------------
# Belépési pont
# ---------------------------------------------------------------------------

def main():
    cfg_path = ROOT / "config.json"
    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)

    if not mt5_connector.connect(cfg):
        sys.exit(1)

    slot_mgr = SlotManager(cfg["trading"]["max_open_slots"])

    try:
        run(cfg, slot_mgr)
    finally:
        mt5_connector.disconnect()


if __name__ == "__main__":
    main()
