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
from core import mt5_visual
from core import risky_mode
from core import correlation
from core.indicator_engine import atr as atr_indicator
from core.risk_manager import calc_lot, calc_effective_slots, SlotManager
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
    point:            Optional[float] = None   # 1 pont ára (MT5 symbol_info.point)
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
    opt_grade:        Optional[tuple] = None  # (minősítő_szöveg, szín-név)
    opt_grade_reason: str  = ""               # mi húzza le a minősítést
    corr_conflict:    bool = False            # korrelált kitettség-ütközés (villog)
    corr_tip:         str  = ""               # ütközés magyarázata (tooltip)
    spread_pts:       int  = 0      # aktuális spread pontban (MT5 egység)
    max_spread_pts:   int  = 0      # megengedett max spread pontban
    timeframe_remaining: dict = field(default_factory=dict)  # {percek: hátralévő mp}

    # ── Stratégia-specifikus cellák: {oszlop_kulcs: (szöveg, szín-név)} ───
    # LIVE párnál a MOTOR tölti a saját jelzésállapotából (strategy.live_cells) →
    # a tábla PONTOSAN azt mutatja, amivel a motor kereskedik. STOPPED/preview
    # párnál a GUI tölti (compute_display). A cells_ts a motor utolsó írásának
    # ideje: ha friss, a GUI nem írja felül a rekonstrukcióval.
    strategy_cells:   dict = field(default_factory=dict)
    cells_ts:         float = 0.0

    # Per-instrumentum vizualizáció ki/be (a GUI V gombja billenti). A motor
    # csak akkor írja a viz-fájlt, ha ez True. Kikapcsoláskor a GUI törli a
    # chart-objektumokat (mt5_visual.clear).
    viz_enabled:      bool = True


# Globális dashboard állapot — a GUI ebből olvas
dashboard: dict[str, PairDashboardState] = {}

# Globális Play/Stop vezérlés — a GUI írja, a run() olvassa
# Értékek: "LIVE" | "STOPPED" | "OPTIMIZING" | "QUEUED"
instrument_state: dict[str, str] = {}

# Optimizer státusz szöveg per pár (progress, "Várakozik...", "Kész ✓")
optimizer_status: dict[str, str] = {}

# Per-ticket pozíció-állapot (GUI ↔ motor megosztott):
#   {ticket: {"original_sl": float, "trailing_enabled": bool, "be_done": bool}}
# A motor tölti/karbantartja; a Pozíciók fül ebből olvas és ezt billenti
# (trailing ki/be, kézi BE jelzése).
position_state: dict[int, dict] = {}

# MT5 chart-vizualizáció: engedélyezés + írásgyakoriság (run() tölti a configból),
# és a per-szimbólum utolsó írás ideje (throttle — ne írjunk minden 10 mp-ben mélyet).
VIZ_ENABLED:      bool  = True
VIZ_INTERVAL_SEC: float = 15.0
_viz_last_write:  dict[str, float] = {}


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
    strategy_name: str = "",
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

    log.info("✅ [%s] %s %s | Lot: %.2f | SL: %.5f | TP: %.5f | Ticket: %d | magic: %d",
             strategy_name or comment, symbol, direction, lot, sl, tp, result.order, magic)
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
    ok = result is not None and result.retcode == mt5.TRADE_RETCODE_DONE
    if not ok:
        # Gyakori ok: az új SL túl közel az árhoz (bróker min. stop-távolság) →
        # a hívó a stops_level figyelembevételével számolja az új SL-t.
        log.debug("%s #%d — SL módosítás elutasítva (%s): %s",
                  pos.symbol, ticket, new_sl,
                  result.comment if result else mt5.last_error())
    return ok


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


# A trades.csv KANONIKUS oszlopsémája (nyitás és zárás egyaránt ezt tölti; a
# hiányzó mezők üresek maradnak). A "strategy" oszlop mondja meg, MELYIK
# stratégia nyitotta a pozíciót.
TRADES_COLUMNS = ["time", "event", "strategy", "symbol", "direction", "lot",
                  "price", "sl", "tp", "ticket", "magic", "pnl_usd"]


def log_trade(row: dict):
    """Egy sor a trades.csv-be, a kanonikus sémára igazítva. Ha a meglévő fájl
    fejléce eltér (régi formátum), egyszer átírja a kanonikus sémára."""
    df = pd.DataFrame([row]).reindex(columns=TRADES_COLUMNS)
    if not TRADES_CSV.exists():
        df.to_csv(TRADES_CSV, index=False)
        return
    try:
        existing_cols = pd.read_csv(TRADES_CSV, nrows=0).columns.tolist()
    except Exception:
        existing_cols = []
    if existing_cols == TRADES_COLUMNS:
        df.to_csv(TRADES_CSV, mode="a", header=False, index=False)
    else:
        # Régi/eltérő fejléc → egyszeri migráció a kanonikus sémára
        try:
            old = pd.read_csv(TRADES_CSV).reindex(columns=TRADES_COLUMNS)
            pd.concat([old, df], ignore_index=True).to_csv(TRADES_CSV, index=False)
        except Exception:
            df.to_csv(TRADES_CSV, mode="a", header=False, index=False)


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
# MT5 chart-vizualizáció (a stratégia visual_objects-je → Common\Files fájl)
# ---------------------------------------------------------------------------

def framework_visual_objects(pair_cfg: dict, md: MarketData) -> list:
    """KERETRENDSZER-szintű viz-objektumok (nem a stratégiáé): a no-trade órák
    SZÜRKE dobozai a `trade_hours` alapján. A stratégia semmit nem tud a
    kereskedési órákról — ezért ezt a keret emittálja (a szeparáció megmarad).

    Idő: a bar-idővel (szerver/chart idő) EGYEZŐ órákban — a live óra-kapuja is
    ehhez igazodik, így a szürke doboz pontosan azt mutatja, mikor NEM kereskedik.
    """
    from strategy import visual as viz
    th = pair_cfg.get("trade_hours")
    if th is None:
        return []
    no_trade = set(range(24)) - {int(h) for h in th}
    df = md.bars.get("M15")
    if not no_trade or df is None or len(df) < 2:
        return []
    start, end = df.index[0], df.index[-1]
    objs: list = []
    day     = pd.Timestamp(start.date(), tz="UTC")
    end_day = pd.Timestamp(end.date(),   tz="UTC")
    while day <= end_day:
        h = 0
        while h < 24:
            if h in no_trade:
                blk = h
                while h < 24 and h in no_trade:
                    h += 1
                t1 = day + pd.Timedelta(hours=blk)
                t2 = day + pd.Timedelta(hours=h)          # exkluzív blokk-vég
                if t2 > start and t1 < end:               # látható tartományra vágva
                    e1 = int(max(t1, start).timestamp())
                    e2 = int(min(t2, end).timestamp())
                    if e2 > e1:
                        objs.append(viz.Rect(name=f"notrade_{e1}", t1=e1, p1=0.0,
                                             t2=e2, p2=1.0, color="gray"))
            else:
                h += 1
        day += pd.Timedelta(days=1)
    return objs


def write_pair_visuals(symbol: str, params: dict, strategy, pip_size: float,
                       pair_cfg: dict = None):
    """A stratégia + keretrendszer rajzolási objektumait kiírja a viz-fájlba. MÉLY
    adatablakot tölt (visual_lookback_bars) — több, mint a jelzés-warmup —, hogy a
    megjelenített előzmény (SMA-szalag) több napra visszamenjen."""
    bars = {}
    for tf in strategy.timeframes():
        n = strategy.visual_lookback_bars(params, tf.label)
        if n <= 0:
            continue
        df = get_candles(symbol, mt5_timeframe(tf.minutes), n)
        if df is None or len(df) < 3:
            return
        bars[tf.label] = df
    if not bars:
        return
    # pip_size a jövőbeli TP/SL-rajzoláshoz (feltétel 3) — a params nem tartalmazza.
    md = MarketData(symbol=symbol, params={**params, "pip_size": pip_size}, bars=bars)
    # A no-trade szürke dobozok ELŐRE kerülnek (a fájlban előbb) → az al-ablakban
    # a szalag/doboz FÖLÖTTE rajzolódik (a szürke a háttér-időoszlop).
    framework = framework_visual_objects(pair_cfg or {}, md)
    mt5_visual.write(symbol, framework + strategy.visual_objects(md))


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

    # --- MT5 chart-vizualizáció (throttle-olva, mély adatablakkal) ---
    # A kereskedési kapuk (session/napi limit/adathiány) ELŐTT: a viz MEGJELENÍTÉS,
    # nem kereskedés — kereskedési szüneten (pl. session-en kívül) is frissüljön,
    # különben a sávok „megállnak" a session végén.
    now_ts = time.time()
    if (VIZ_ENABLED and ds.viz_enabled
            and now_ts - _viz_last_write.get(symbol, 0.0) >= VIZ_INTERVAL_SEC):
        _viz_last_write[symbol] = now_ts
        try:
            # A viz a LEGFRISSEBB JSON-paramétert használja → ha az instrumentum-
            # ablakban átírod a paramétereket (Mentés), a chart-rajz KÖVETI (a
            # következő viz-ciklusban). A KERESKEDÉS ellenben marad a Play-kori
            # `state.params`-nál (nyitott pozíciót ne zavarjon meg) — az a következő
            # Play-nél frissül. A V ki/be azonnal újrarajzoltat.
            viz_params = load_pair_params(symbol) or params
            write_pair_visuals(symbol, viz_params, strategy, pip_size, pair_cfg)
        except Exception as e:
            log.debug("%s — viz írás hiba: %s", symbol, e)

    # Óra-kapu: az engedélyezett órák LISTÁJA (trade_hours), amit kézzel állítasz
    # az óránkénti P&L-bontás alapján. Ha nincs trade_hours → visszaesik a régi
    # sess_start/sess_end TARTOMÁNYRA (visszafelé kompatibilis).
    #
    # Az óra a SZERVER/CHART idő (a bar-idővel és az óránkénti bontással egyező),
    # NEM a valós UTC — így a trade_hours pontosan azt jelenti, amit a charton
    # látsz, és a no-trade szürke sáv is illeszkedik. (A tick.time szerver-epoch.)
    _tick = mt5.symbol_info_tick(symbol)
    hour = (datetime.fromtimestamp(_tick.time, tz=timezone.utc).hour
            if _tick else datetime.now(timezone.utc).hour)
    trade_hours = pair_cfg.get("trade_hours")
    if trade_hours is not None:
        if hour not in {int(h) for h in trade_hours}:
            return
    else:
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

    # A tábla jelzés-celláit a MOTOR ÉLŐ állapotából töltjük (nem külön
    # rekonstrukcióból) → a kijelzés PONTOSAN azt mutatja, amivel kereskedünk.
    try:
        cells = strategy.live_cells(state.strat_state, md)
        ds.strategy_cells = {k: (c.text, c.color) for k, c in cells.items()}
        ds.cells_ts = time.time()
    except Exception:
        pass

    # --- Pozícióterv (méretezés) a STRATÉGIÁTÓL + spread-kapu ─────────────
    # Konvenció: az első deklarált időkeret a "fő" (magasabb). Az SL/TP-méretet a
    # STRATÉGIA adja a `sl_tp_pips` hookban a SAJÁT indikátoraiból → a motor
    # stratégia-független (nem ismer 'atr'-t). A stratégia indikátor-sorát a
    # `bt_indicators`-ból vesszük (ugyanaz, amit a backtest lát).
    primary = strategy.timeframes()[0].label
    df_primary = bars[primary]
    tfs = strategy.timeframes()
    df_lo = bars.get(tfs[1].label, df_primary) if len(tfs) > 1 else df_primary
    try:
        _hi_ind, _ = strategy.bt_indicators(df_primary, df_lo, params)
        hi_row = _hi_ind.iloc[-2] if len(_hi_ind) >= 2 else None
    except Exception:
        hi_row = None

    # A spread-kapuhoz volatilitás-mérték (ATR): ez KERETRENDSZER-szintű
    # végrehajtási kapu (a bróker spread-je vs. a piac mozgékonysága), NEM
    # stratégia-jelzés — generikus ATR-t számol a core-ból.
    atr_ser = atr_indicator(df_primary["high"], df_primary["low"],
                            df_primary["close"], params.get("atr_period", 14))
    atr_val = atr_ser.iloc[-2] if len(atr_ser) >= 2 else float("nan")
    if pd.isna(atr_val):
        atr_val = None

    spread_ok = True
    sym_info  = mt5.symbol_info(symbol)
    if sym_info and atr_val is not None and sym_info.point > 0:
        current_spread_pts = sym_info.spread
        atr_pts    = int(atr_val / sym_info.point)
        ratio      = params.get("max_spread_atr_ratio", 0.20)
        pip_to_pt  = max(1, round(pip_size / sym_info.point))
        min_pts    = max(1, int(params.get("min_spread_pips", 2.0) * pip_to_pt))
        max_spread_pts = max(min_pts, int(atr_pts * ratio))
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

        # Megosztott pozíció-állapot (GUI ↔ motor): eredeti SL, trailing toggle, BE,
        # trail_points (None = az optimalizált paramétert használjuk; egész = kézi
        # felülírás PONTBAN a Pozíciók fülről), trail_moved (a trailing már húzott-e).
        pstate = position_state.setdefault(ticket, {
            "original_sl": pos.sl, "trailing_enabled": True, "be_done": is_rf,
            "trail_points": None, "trail_moved": False})
        # Kézi BE (a Pozíciók fülről): ha be_done jelölt, de a slot-kezelő még
        # nem tudja, szinkronizáljuk (slot felszabadul, trailing indulhat).
        if pstate.get("be_done") and not is_rf:
            slot_mgr.set_risk_free(ticket)
            is_rf = True

        # Breakeven ellenőrzés (risky módban AZONNAL, amint profitban van).
        # A tényleges SL nem pontos BE, hanem BE + spread puffer (lásd
        # mt5_connector.move_to_breakeven): spread×2 → ×1 → pontos BE fallback.
        be_pct = params.get("breakeven_pct", 0.5)
        if (risky or be_pct > 0) and not is_rf:
            if pos.type == mt5.ORDER_TYPE_BUY:
                be_price = (pos.price_open if risky
                            else pos.price_open + (pos.tp - pos.price_open) * be_pct)
                trigger = pos.price_current >= be_price
            else:
                be_price = (pos.price_open if risky
                            else pos.price_open - (pos.price_open - pos.tp) * be_pct)
                trigger = pos.price_current <= be_price
            if trigger and mt5_connector.move_to_breakeven(ticket):
                slot_mgr.set_risk_free(ticket)
                pstate["be_done"] = True
                log.info("✦ %s #%d — költség-tudatos breakeven beállítva%s", symbol, ticket,
                         " (risky)" if risky else "")

        # Trailing stop (kockázatmentes után, és csak ha kézzel nincs kikapcsolva).
        # MINDEN számítás PONTBAN (bróker-egység), hogy a kijelzés és a motor
        # egyértelmű legyen (a "pip" félreérthető volt).
        is_rf = slot_mgr.is_risk_free(ticket)
        if is_rf and pstate.get("trailing_enabled", True):
            point  = sym_info.point if (sym_info and sym_info.point > 0) else pip_size
            digits = sym_info.digits if sym_info else 5

            # Követési távolság ÁR-ban:
            #   • kézi felülírás (Pozíciók fül) PONTBAN → pontos érték, risky NEM felezi
            #   • egyébként az optimalizált trail_distance_pips → ár, risky felezi
            override_points = pstate.get("trail_points")
            if override_points is not None:
                dist_price = override_points * point
            else:
                base_pips  = params.get("trail_distance_pips", 6)
                dist_price = pip_to_price(base_pips, pip_size) * (0.5 if risky else 1.0)

            # A bróker MINIMUM stop-távolsága: ha az új SL ennél közelebb esne az
            # árhoz, a modify csendben elutasításra kerül → a trailing sosem fog
            # profitot. Ezért az effektív követés a min. stop-távolság + 1 pont alá
            # NEM mehet — így a legkorábbi (kb. 1 pontnyi) profitot is lekötjük.
            min_stop_price = (sym_info.trade_stops_level * point) if sym_info else 0.0
            eff_price = max(dist_price, min_stop_price + point)

            # Risky mód: a trailing AZONNAL induljon (nem várunk aktiválási profitra).
            act_price = 0.0 if risky else pip_to_price(
                params.get("trail_activation_pips", 8), pip_size)

            if pos.type == mt5.ORDER_TYPE_BUY:
                if pos.price_current >= pos.price_open + act_price:
                    new_sl = round(pos.price_current - eff_price, digits)
                    if new_sl > pos.sl and modify_sl(ticket, new_sl):
                        pstate["trail_moved"] = True
                        log.info("↗ %s #%d trailing SL → %.*f (%d pont követés%s)",
                                 symbol, ticket, digits, new_sl,
                                 round(eff_price / point), ", risky" if risky else "")
            else:
                if pos.price_current <= pos.price_open - act_price:
                    new_sl = round(pos.price_current + eff_price, digits)
                    if new_sl < pos.sl and modify_sl(ticket, new_sl):
                        pstate["trail_moved"] = True
                        log.info("↘ %s #%d trailing SL → %.*f (%d pont követés%s)",
                                 symbol, ticket, digits, new_sl,
                                 round(eff_price / point), ", risky" if risky else "")

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
                # Csak a SAJÁT szimbólum ticketjeit kezeljük — másik pár
                # feldolgozási körében ne írjuk rá ennek P&L-jét a wrong ds/state-re.
                # (Ez okozta az UKOUSD zárt kereskedés P&L-jének EURUSD sorba kerülését.)
                if last_deal.symbol != symbol:
                    continue
                pnl = last_deal.profit + last_deal.commission + last_deal.swap
                state.daily_pnl += pnl
                ds.daily_pnl     = state.daily_pnl
                ds.position_pnl  = None
                ds.risk_free      = False
                slot_mgr.remove(ticket)
                position_state.pop(ticket, None)
                log.info("📋 [%s] %s #%d zárt | P&L: %.2f$",
                         strategy.name, symbol, ticket, pnl)
                log_trade({
                    "time":     datetime.now(timezone.utc).isoformat(),
                    "event":    "close",
                    "strategy": strategy.name,
                    "symbol":   symbol,
                    "ticket":   ticket,
                    "magic":    magic,
                    "pnl_usd":  pnl,
                })

    # --- Belépés a stratégia jelzése alapján ---
    # Egy szimbólumon EGYSZERRE csak egy pozíció lehet — soha ne halmozzon
    # ugyanarra a párra (ez okozta a 8 GBPAUD-pozíciót 4 slotra).
    already_open = len(symbol_positions) > 0
    # Diagnosztika: ha a stratégia JELET adott (a viz ezt rajzolja — a NYERS
    # jelet, végrehajtási szűrők nélkül), de egy VÉGREHAJTÁSI kapu blokkol, írjuk
    # ki, MELYIK — különben úgy tűnik, "látta a jelet, mégsem lépett be". A jel
    # pillanatnyi (az adott M1 átütés gyertyáján), ezért ez ritkán logol.
    if signal != "NONE":
        _block = ("már van nyitott pozíció ezen a páron" if already_open else
                  "nincs érvényes ATR (adathiány)" if atr_val is None else
                  "nincs szabad slot" if not slot_mgr.can_open() else
                  "spread túl nagy (piac-kapu)" if not spread_ok else None)
        if _block:
            log.info("⏭ %s %s jel — belépő KIHAGYVA: %s", symbol, signal, _block)

    if (signal != "NONE" and not already_open and atr_val is not None
            and slot_mgr.can_open() and spread_ok):
        # ── Korreláció / devizakitettség kapu ──────────────────────────────
        cmode    = correlation.get_mode()
        conflict = []
        if cmode != correlation.INACTIVE:
            others = [(p.symbol, "BUY" if p.type == mt5.ORDER_TYPE_BUY else "SELL")
                      for p in open_positions if p.symbol != symbol]
            conflict = correlation.shared_exposure(symbol, signal, others)

        if conflict and cmode == correlation.STRONGER:
            # Az erősebb (jobb minőségű) nyit elsőként — a run() minőség szerint
            # rendezve dolgozza fel a párokat —, a korrelált újat blokkoljuk.
            log.info("K-blokk: %s belépés kihagyva (azonos kitettség: %s)",
                     symbol, ", ".join(conflict))
        else:
            ctf = risk_trading_cfg
            if conflict and cmode == correlation.HALF:
                ctf = {**ctf, "account_risk_pct": ctf["account_risk_pct"] * 0.5}
                log.info("K: %s fél mérettel (azonos kitettség: %s)",
                         symbol, ", ".join(conflict))
            # A méretezést a STRATÉGIA adja (SL/TP pip) — None → nincs érvényes
            # méret, kihagyjuk a belépőt.
            plan = strategy.sl_tp_pips(hi_row, params, pip_size) if hi_row is not None else None
            if plan is None:
                log.info("⏭ %s %s jel — belépő KIHAGYVA: a stratégia nem adott "
                         "érvényes SL/TP méretet (hi_row/indikátor hiány).", symbol, signal)
                return
            sl_pips, tp_pips = plan
            eff_slots = calc_effective_slots(balance, sl_pips, pair_cfg, ctf)
            lot = calc_lot(balance, sl_pips, pair_cfg, ctf, eff_slots)

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

                ticket = open_position(symbol, signal, lot, sl_price, tp_price, magic,
                                       comment=strategy.name, strategy_name=strategy.name)
                if ticket:
                    slot_mgr.add(ticket)
                    log_trade({
                        "time":      datetime.now(timezone.utc).isoformat(),
                        "event":     "open",
                        "strategy":  strategy.name,
                        "symbol":    symbol,
                        "direction": signal,
                        "lot":       lot,
                        "price":     open_price,
                        "sl":        sl_price,
                        "tp":        tp_price,
                        "ticket":    ticket,
                        "magic":     magic,
                    })


# ---------------------------------------------------------------------------
# Fő ciklus
# ---------------------------------------------------------------------------

def run(cfg: dict, slot_mgr: SlotManager):
    global VIZ_ENABLED, VIZ_INTERVAL_SEC
    trading_cfg = cfg["trading"]
    viz_cfg     = cfg.get("visualization", {})
    VIZ_ENABLED      = viz_cfg.get("enabled", True)
    VIZ_INTERVAL_SEC = viz_cfg.get("interval_sec", 15.0)
    strategy    = get_strategy(cfg)
    magic       = strategy.magic(cfg)   # a stratégia magicje (alap: broker.magic)
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
            viz_enabled=pair_cfg.get("viz_enabled", True),   # V mód a config.json-ból
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
    from strategy.settings import load_config
    cfg = load_config(ROOT / "config.json")

    if not mt5_connector.connect(cfg):
        sys.exit(1)

    slot_mgr = SlotManager(cfg["trading"]["max_open_slots"])

    try:
        run(cfg, slot_mgr)
    finally:
        mt5_connector.disconnect()


if __name__ == "__main__":
    main()
