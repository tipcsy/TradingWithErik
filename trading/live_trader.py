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
from core import risky_mode
from core.indicator_engine import atr as atr_indicator
from core.risk_manager import calc_sl_tp_pips, calc_lot, calc_effective_slots, SlotManager
from strategy import get_strategy
from strategy.base import MarketData

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
    pos_count:        int  = 0                  # nyitott pozíciók száma e szimbólumon
    risk_free:        bool = False
    risky:            bool = False              # Risky mód aktív-e (instabil piac)
    daily_pnl:        float = 0.0
    enabled:          bool = True
    trained:          bool = False  # van-e optimalizált paraméter
    spread_pts:       int  = 0      # aktuális spread pontban (MT5 egység)
    max_spread_pts:   int  = 0      # megengedett max spread pontban
    timeframe_remaining: dict = field(default_factory=dict)  # {percek: hátralévő mp}

    # ── Stratégia-specifikus cellák: {oszlop_kulcs: (szöveg, szín-név)} ───
    # A stratégia tölti (compute_display); a GUI csak rajzolja. Stratégiacsere
    # NEM igényli e dataclass módosítását.
    strategy_cells:   dict = field(default_factory=dict)


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

def mt5_timeframe(minutes: int):
    """Perc → MT5 timeframe konstans (stratégia-időkeretek feloldásához)."""
    table = {
        1:   mt5.TIMEFRAME_M1,   5:   mt5.TIMEFRAME_M5,
        15:  mt5.TIMEFRAME_M15,  30:  mt5.TIMEFRAME_M30,
        60:  mt5.TIMEFRAME_H1,   240: mt5.TIMEFRAME_H4,
    }
    return table.get(minutes, mt5.TIMEFRAME_M1)


@dataclass
class LivePairState:
    symbol:      str
    pair_cfg:    dict
    params:      dict
    trading_cfg: dict
    magic:       int
    strat_state: object = None    # a stratégia jelzésállapota (átlátszatlan)
    daily_pnl:   float  = 0.0
    daily_date:  str    = ""


# ---------------------------------------------------------------------------
# Fő kereskedési ciklus
# ---------------------------------------------------------------------------

def process_pair(state: LivePairState, slot_mgr: SlotManager, balance: float,
                 strategy):
    """Egy LIVE pár feldolgozása.

    A JELZÉS a stratégiától jön (strategy.on_bar_close, ZÁRT gyertyán). A
    pozíció-méretezés, SL/TP, pozíciókezelés és végrehajtás a motor/kockázati
    réteg felelőssége — stratégia-független.
    """
    symbol     = state.symbol
    pair_cfg   = state.pair_cfg
    params     = state.params
    trading_cfg = state.trading_cfg
    magic      = state.magic
    pip_size   = pair_cfg["pip_size"]

    # Dashboard state (a megjelenítendő cellákat a GUI tölti — itt csak
    # végrehajtási tények: pozíció P&L, napi P&L).
    ds = dashboard.setdefault(symbol, PairDashboardState(symbol=symbol, trained=True))

    # Risky mód: instabil piac → óvatosabb kockázat. Hatások:
    #   • feleződő kockázat (account_risk_pct × 0.5), amennyiben a min_lot engedi
    #   • azonnali SL→BE (amint profitban van)
    #   • feleződő trailing-távolság
    risky = risky_mode.is_risky(symbol)
    ds.risky = risky
    risk_trading_cfg = (
        {**trading_cfg, "account_risk_pct": trading_cfg["account_risk_pct"] * 0.5}
        if risky else trading_cfg)

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

    # --- Piaci adat a stratégia időkereteire ---
    bars = {}
    for tf in strategy.timeframes():
        wu = strategy.warmup_bars(params, tf.label)
        df = get_candles(symbol, mt5_timeframe(tf.minutes), wu)
        if df is None or len(df) < wu:
            return
        bars[tf.label] = df
    md = MarketData(symbol=symbol, params=params, bars=bars)

    # --- Jelzés a stratégiától (ZÁRT gyertyán, állapottartó) ---
    state.strat_state, signal = strategy.on_bar_close(state.strat_state, md)

    # --- ATR (méretezéshez) + spread (kapuhoz) — végrehajtási inputok ---
    # Konvenció: az első deklarált időkeret a "fő" (magasabb) — ezen mérünk ATR-t.
    primary = strategy.timeframes()[0].label
    df_primary = bars[primary]
    atr_ser = atr_indicator(df_primary["high"], df_primary["low"],
                            df_primary["close"], params.get("atr_period", 14))
    atr_val = atr_ser.iloc[-2] if len(atr_ser) >= 2 else float("nan")
    if pd.isna(atr_val):
        atr_val = None

    spread_ok = True
    sym_info  = mt5.symbol_info(symbol)
    if sym_info and atr_val is not None and sym_info.point > 0:
        current_spread_pts = sym_info.spread
        atr_pts = int(atr_val / sym_info.point)
        ratio   = params.get("max_spread_atr_ratio", 0.20)
        max_spread_pts = max(1, int(atr_pts * ratio))
        spread_ok = (current_spread_pts <= max_spread_pts)
        if not spread_ok:
            log.debug("%s — spread túl nagy: %d pt > %d pt max, kihagyva.",
                      symbol, current_spread_pts, max_spread_pts)

    # --- Pozíció menedzsment ---
    open_positions = get_open_positions(magic)
    symbol_positions = [p for p in open_positions if p.symbol == symbol]

    for pos in symbol_positions:
        ticket = pos.ticket
        pnl    = pos.profit
        is_rf  = slot_mgr.is_risk_free(ticket)

        # Breakeven ellenőrzés (risky módban AZONNAL, amint profitban van)
        be_pct = params.get("breakeven_pct", 0.5)
        if (risky or be_pct > 0) and not is_rf:
            if pos.type == mt5.ORDER_TYPE_BUY:
                be_price = (pos.price_open if risky
                            else pos.price_open + (pos.tp - pos.price_open) * be_pct)
                if pos.price_current >= be_price:
                    if modify_sl(ticket, pos.price_open):
                        slot_mgr.set_risk_free(ticket)
                        log.info("✦ %s #%d — breakeven beállítva%s", symbol, ticket,
                                 " (risky)" if risky else "")
            else:
                be_price = (pos.price_open if risky
                            else pos.price_open - (pos.price_open - pos.tp) * be_pct)
                if pos.price_current <= be_price:
                    if modify_sl(ticket, pos.price_open):
                        slot_mgr.set_risk_free(ticket)
                        log.info("✦ %s #%d — breakeven beállítva%s", symbol, ticket,
                                 " (risky)" if risky else "")

        # Trailing stop (csak kockázatmentes után); risky módban feleződő távolság
        is_rf = slot_mgr.is_risk_free(ticket)
        if is_rf:
            trail_act  = params.get("trail_activation_pips", 8)
            trail_dist = params.get("trail_distance_pips", 6) * (0.5 if risky else 1.0)
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

    # Lezárt pozíciók észlelése — a GLOBÁLIS nyitott halmazhoz mérve!
    # (Korábbi hiba: az aktuális szimbólum pozícióihoz mértünk, így egy MÁSIK
    #  pár feldolgozása tévesen "lezártnak" vette és kivette e pár ticketjeit a
    #  slot-kezelőből → felszabadult a slot → korlátlan túlnyitás ugyanarra a párra.)
    all_open_tickets = {p.ticket for p in open_positions}
    for ticket in slot_mgr.all_tickets():
        still_open = ticket in all_open_tickets
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

    # --- Belépés a stratégia jelzése alapján ---
    # Egy szimbólumon EGYSZERRE csak egy pozíció lehet — soha ne halmozzon
    # ugyanarra a párra (ez okozta a 8 GBPAUD-pozíciót 4 slotra).
    already_open = len(symbol_positions) > 0
    if (signal != "NONE" and not already_open and atr_val is not None
            and slot_mgr.can_open() and spread_ok):
        sl_pips, tp_pips = calc_sl_tp_pips(atr_val, {**params, "pip_size": pip_size})
        eff_slots = calc_effective_slots(balance, sl_pips, pair_cfg, risk_trading_cfg)
        lot = calc_lot(balance, sl_pips, pair_cfg, risk_trading_cfg, eff_slots)

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


# ---------------------------------------------------------------------------
# Fő ciklus
# ---------------------------------------------------------------------------

def run(cfg: dict, slot_mgr: SlotManager):
    magic       = cfg["broker"]["magic"]
    trading_cfg = cfg["trading"]
    strategy    = get_strategy(cfg)
    risky_mode.load()                      # induló risky állapot
    last_risky_reload = time.time()
    risky_reload_sec  = cfg.get("trading", {}).get("risky_reload_sec", 3600)

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
                strat_state=strategy.new_signal_state(symbol),
            )
            log.info("%s — LIVE (params betöltve)", symbol)
        else:
            instrument_state[symbol] = "STOPPED"
            if not trained:
                log.info("%s — STOPPED (nincs params, futtatsd az optimalizálást)", symbol)

    # ── Induló helyreállítás (újraindítás után) ──────────────────────────
    # A bot a MAGIC szám alapján megtalálja a saját, már nyitott pozícióit:
    #   1) felveszi őket a slot-kezelőbe (ne nyisson a limit fölé),
    #   2) az SL helyzetéből kikövetkezteti a kockázatmentes állapotot
    #      (ha az SL már az entry-n túl van profit-irányban → risk-free),
    #   3) a nyitott pozíciójú párokat LIVE-ba teszi, hogy a motor tovább
    #      kezelje őket (breakeven, trailing, zárás-detektálás).
    recovered_syms: set[str] = set()
    for _p in get_open_positions(magic):
        slot_mgr.add(_p.ticket)
        if _p.sl and _p.sl != 0.0:
            if _p.type == mt5.ORDER_TYPE_BUY and _p.sl >= _p.price_open:
                slot_mgr.set_risk_free(_p.ticket)
            elif _p.type == mt5.ORDER_TYPE_SELL and _p.sl <= _p.price_open:
                slot_mgr.set_risk_free(_p.ticket)
        recovered_syms.add(_p.symbol)

    for _sym in recovered_syms:
        _pcfg = all_pairs.get(_sym)
        if not isinstance(_pcfg, dict) or _sym in pair_states:
            continue
        _params = load_pair_params(_sym)
        if _params:
            pair_states[_sym] = LivePairState(
                symbol=_sym, pair_cfg=_pcfg, params=_params,
                trading_cfg=trading_cfg, magic=magic,
                strat_state=strategy.new_signal_state(_sym))
            instrument_state[_sym] = "LIVE"
            log.info("%s — helyreállítva LIVE-ba (nyitott pozíció a magic alatt)", _sym)
        else:
            log.warning("%s — nyitott pozíció, de nincs params! Kézi kezelés szükséges.",
                        _sym)

    if slot_mgr.all_tickets():
        rf = sum(1 for t in slot_mgr.all_tickets() if slot_mgr.is_risk_free(t))
        log.info("Induláskor %d nyitott pozíció helyreállítva (%d kockázatmentes).",
                 len(slot_mgr.all_tickets()), rf)

    log.info("Élő kereskedés indul | %d LIVE pár", len(pair_states))

    while True:
        try:
            balance = mt5_connector.account_balance()

            # Risky állapot óránkénti újraolvasása (külső program írhatja)
            if time.time() - last_risky_reload >= risky_reload_sec:
                risky_mode.load()
                last_risky_reload = time.time()

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
                            strat_state=strategy.new_signal_state(symbol),
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
                    process_pair(pair_states[symbol], slot_mgr, balance, strategy)

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
