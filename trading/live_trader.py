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

# Stratégia-hatókörű params-tárolás (közös, könnyű modul — nincs optimizer/optuna
# függés). Az aktív stratégiát a run() állítja be a config alapján.
from core.params_store import (
    PARAMS_DIR, params_file, set_active_strategy, migrate_flat_layout,
)
TRADES_CSV   = ROOT / "trades.csv"


def load_pair_params(symbol: str, strategy_name: str | None = None) -> Optional[dict]:
    """Per-pár params betöltése: data/optimized_params/<strategy>/<SYMBOL>.json.
    `strategy_name=None` → az aktív stratégia (a run() beállítja); több-stratégia
    esetén a hívó a konkrét stratégiát adja."""
    f = params_file(symbol, strategy_name)
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
    # Több-stratégia: a stratégia-példány (a motor ezen ciklusozik páronként) és
    # hogy EZ a stratégia írja-e a dashboard cella-kijelzését (elsődleges) — így
    # több stratégia nem írja felül egymás jelölő-köreit ugyanazon a soron.
    strategy:    object = None
    is_display:  bool   = True


# ---------------------------------------------------------------------------
# MT5 chart-vizualizáció (a stratégia visual_objects-je → Common\Files fájl)
# ---------------------------------------------------------------------------

# SL-mozgás napló: az MT5 NEM tárolja a pozíció SL-módosításait (breakeven/
# trailing), ezért mi naplózzuk — így a viz lépcsős SL-vonalat tud rajzolni (eredeti
# piros, elmozdított sárga). Soronként: position_id;szerver_epoch;sl.
SL_MOVES_DIR = ROOT / "data" / "sl_moves"


def sl_journal_append(symbol: str, ticket: int, t_server: int, sl: float) -> None:
    """Egy SL-mozgás hozzáfűzése a `data/sl_moves/<SYM>.csv`-hez. Az idő a
    `pos.time_update` (a módosítás SZERVER-ideje) → egyezik a gyertya-idővel."""
    try:
        SL_MOVES_DIR.mkdir(parents=True, exist_ok=True)
        with open(SL_MOVES_DIR / f"{symbol}.csv", "a", encoding="ascii") as fh:
            fh.write(f"{int(ticket)};{int(t_server)};{repr(float(sl))}\n")
    except Exception:
        pass


def sl_journal_read(symbol: str) -> dict:
    """position_id → [(szerver_epoch, sl), …] idő szerint rendezve. {} ha nincs."""
    path = SL_MOVES_DIR / f"{symbol}.csv"
    out: dict = {}
    if not path.exists():
        return out
    try:
        for ln in path.read_text(encoding="ascii", errors="replace").splitlines():
            parts = ln.split(";")
            if len(parts) != 3:
                continue
            out.setdefault(int(parts[0]), []).append((int(parts[1]), float(parts[2])))
    except Exception:
        return {}
    for pid in out:
        out[pid].sort()
    return out


def apply_no_trade(objects: list, pair_cfg: dict) -> list:
    """KERETRENDSZER-szintű no-trade MASZKOLÁS a `trade_hours` alapján. A stratégia
    az órákról nem tud — a KÜLDŐ (keret) alkalmazza a saját szabályát:

      • per-gyertya `BarState`: no-trade órában notrade=1 ÉS dir=0, window=0 →
        a TradeForgeBands al-ablak ott CSAK a szürke sávot mutatja (trend/kék el);
      • belépő-jelölések (`VLine` = m1sig, `Trend` = entry/TP/SL): no-trade órában
        KIMARADNAK — ott a motor úgysem kötne, a jelölés félrevezető lenne.

    A Viz „buta" marad (mindent úgy rajzol, ahogy kap); a szeparáció megmarad: a
    stratégia NYERS jelét a keret a kereskedési-óra szabályával fedi el. Az óra a
    SZERVER/CHART idő UTC epochból — ugyanaz, amivel a live `process_pair` kapuz.
    """
    from strategy import visual as viz
    th = pair_cfg.get("trade_hours")
    if th is None:
        return objects
    no_trade = set(range(24)) - {int(h) for h in th}
    if not no_trade:
        return objects

    def _hour(t: int) -> int:
        return datetime.fromtimestamp(int(t), tz=timezone.utc).hour

    result: list = []
    for o in objects:
        if isinstance(o, viz.BarState):
            if _hour(o.t) in no_trade:
                o.notrade = 1
                o.dir = 0
                o.window = 0
            result.append(o)
        elif isinstance(o, viz.VLine):
            if _hour(o.t1) in no_trade:      # belépő-vonal — no-trade órában elhagyjuk
                continue
            result.append(o)
        elif isinstance(o, viz.Trend):
            mid = (int(o.t1) + int(o.t2)) // 2   # a belépőre centrált → a közepe a belépő ideje
            if _hour(mid) in no_trade:
                continue
            result.append(o)
        else:
            result.append(o)
    return result


def actual_trade_objects(symbol: str, since_ts: int) -> list:
    """A VALÓS kötések (MT5 deal-history) rétege — a replay jel-vonalaktól eltérő:
      • belépő-NYÍL a tényleges betöltési áron (fel=BUY / le=SELL),
      • valós SL (piros) és TP (zöld) SZAGGATOTT vonal a belépőtől a záró idejéig.

    Az SL/TP forrása: NYITOTT pozíció → aktuális (breakeven/trailing utáni) szint;
    LEZÁRT trade → a nyitó order kezdeti SL/TP-je. `since_ts`-től. A réteg NEM esik a
    no-trade maszkolás alá (megtörtént kötés mindig látszik). `deal.time` szerver-
    epoch (mint a gyertya-idő) → a gyertyára illik.
    """
    from strategy import visual as viz
    from datetime import timedelta
    dt_from = datetime.fromtimestamp(int(since_ts), tz=timezone.utc)
    dt_to   = datetime.now(timezone.utc) + timedelta(days=1)
    try:
        with mt5_connector.MT5_LOCK:
            deals     = mt5.history_deals_get(dt_from, dt_to, group=f"*{symbol}*")
            orders    = mt5.history_orders_get(dt_from, dt_to, group=f"*{symbol}*")
            positions = mt5.positions_get(symbol=symbol)
    except Exception:
        deals = orders = positions = None
    if not deals:
        return []

    # position_id → KEZDETI SL/TP (nyitó order, legkorábbi BUY/SELL) és → AKTUÁLIS
    # SL/TP (nyitott pozíció). A kezdeti a lépcsős SL baseline-ja; az aktuális a TP-hez
    # (és fallback SL-hez, ha nincs napló).
    init_sltp: dict = {}
    for o in sorted(orders or [], key=lambda x: x.time_setup):
        if o.type in (mt5.ORDER_TYPE_BUY, mt5.ORDER_TYPE_SELL) and o.position_id not in init_sltp:
            init_sltp[o.position_id] = (float(o.sl), float(o.tp))
    cur_sltp: dict = {p.identifier: (float(p.sl), float(p.tp)) for p in (positions or [])}

    # position_id → záró idő (a legutolsó OUT deal); ha nincs, a pozíció még nyitva.
    close_t: dict = {}
    for d in deals:
        if d.entry == mt5.DEAL_ENTRY_OUT:
            close_t[d.position_id] = max(int(d.time), close_t.get(d.position_id, 0))
    now_ts   = int(datetime.now(timezone.utc).timestamp())
    sl_moves = sl_journal_read(symbol)                 # position_id → [(idő, sl), …]

    objs: list = []
    for d in deals:
        if d.symbol != symbol or d.entry != mt5.DEAL_ENTRY_IN:
            continue                                   # csak a BELÉPŐ (nyitó) deal-ek
        if d.type not in (mt5.DEAL_TYPE_BUY, mt5.DEAL_TYPE_SELL):
            continue
        t = int(d.time)
        if t < int(since_ts):
            continue
        pid    = d.position_id
        is_buy = (d.type == mt5.DEAL_TYPE_BUY)
        t_end  = close_t.get(pid, now_ts)
        if t_end <= t:
            t_end = t + 6 * 60                          # legalább pár gyertyányit húzzunk

        objs.append(viz.Arrow(
            name=f"deal_{d.ticket}", t1=t, p1=float(d.price),
            code=233 if is_buy else 234,               # fel = BUY, le = SELL
            color="lime" if is_buy else "orange", width=2))

        # ── Lépcsős SL: baseline (kezdeti, orderből) + a naplózott mozgások ──
        # Az első szakasz PIROS (eredeti SL), minden elmozdított szakasz SÁRGA — a
        # tényleges időszakában (belépő→1. mozgás, majd mozgásról mozgásra → záró/most).
        init_sl = init_sltp.get(pid, (0.0, 0.0))[0]
        pts: list = []
        if init_sl and init_sl > 0:
            pts.append((t, init_sl))
        for mt_, msl in sl_moves.get(pid, []):
            if msl and msl > 0 and t <= mt_ <= t_end:
                pts.append((int(mt_), float(msl)))
        if not pts:                                    # se order-SL, se napló → aktuális szint
            cur_sl = cur_sltp.get(pid, (0.0, 0.0))[0]
            if cur_sl and cur_sl > 0:
                pts.append((t, cur_sl))
        for i, (st_, sl_) in enumerate(pts):
            en_ = pts[i + 1][0] if i + 1 < len(pts) else t_end
            if en_ <= st_:
                en_ = st_ + 6 * 60
            objs.append(viz.Trend(
                name=f"dealsl_{d.ticket}_{i}", t1=st_, p1=sl_, t2=en_, p2=sl_,
                color="red" if i == 0 else "yellow", width=1, style=1))

        # ── TP (nem lépcsős): aktuális, ha nyitva, különben a kezdeti ──
        tp = cur_sltp.get(pid, init_sltp.get(pid, (0.0, 0.0)))[1]
        if tp and tp > 0:
            objs.append(viz.Trend(name=f"dealtp_{d.ticket}", t1=t, p1=tp, t2=t_end, p2=tp,
                                  color="green", width=1, style=1))
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
    # A stratégia per-gyertya BarState-jeire a keret RÁMASZKOLJA a no-trade órákat
    # (a szürke sáv + a trend/kék eltüntetése ott) — a Viz csak megjeleníti.
    objects = apply_no_trade(strategy.visual_objects(md), pair_cfg or {})
    # + a VALÓS kötések nyilai ugyanarra az M1-ablakra (a replay-jelek mellé, hogy
    # látszódjon, melyik jelből lett tényleges trade). A maszkolás UTÁN fűzzük hozzá.
    m1 = bars.get("M1")
    if m1 is not None and len(m1):
        objects = objects + actual_trade_objects(symbol, int(m1.index[0].timestamp()))
    mt5_visual.write(symbol, objects)


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

    # Kockázatcsökkentő PRESET (per-pár, data/risk_mode.json) — a backtesttel
    # AZONOS modell:
    #   • off    : sima BE (breakeven_pct) + trailing
    #   • risky  : felezett méret + azonnali BE + azonnali/felezett trailing
    #   • halving/shield : 1R-nél RÉSZLEGES ZÁRÁS (Felező 50% / Pajzs 75%) + runner-
    #     stop (keep|breakeven|trailing). A részleges zárás után a pozíció
    #     kockázatmentes (a lezárt profit fedezi a runner max veszteségét).
    from core import rr_state, risk_reduction as _rr
    _spec    = rr_state.spec_for(symbol)
    _preset  = _spec.get("preset", _rr.PRESET_OFF)
    _cautious = bool(_spec.get("cautious", False))
    risky    = (_preset == _rr.PRESET_RISKY)      # a régi BE/trailing ág ehhez igazodik
    ds.risky = risky
    ds.rr_preset = _preset
    # Óvatos (felezett) méret: risky preset VAGY kézi 'cautious' override.
    risk_trading_cfg = (
        {**trading_cfg, "account_risk_pct": trading_cfg["account_risk_pct"] * 0.5}
        if _cautious else trading_cfg)

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
    # Több stratégia esetén CSAK az elsődleges (is_display) írja a soron a köröket,
    # hogy ne írják felül egymást (a per-stratégia oszlopok az A4-ben jönnek).
    if getattr(state, "is_display", True):
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

        # SL-mozgás naplózása a viz lépcsős SL-vonalához: ha a pos.sl változott az
        # utóbb látott értékhez képest, feljegyezzük (idő = pos.time_update = a
        # módosítás SZERVER-ideje). Forrás-független (breakeven/trailing/kézi). Első
        # észleléskor csak rögzítjük a kiindulást (nem naplózunk → nincs restart-
        # duplikáció; a baseline-t a rajzoló a nyitó orderből veszi).
        _prev_sl = pstate.get("last_sl")
        if _prev_sl is None:
            pstate["last_sl"] = pos.sl
        elif abs(pos.sl - _prev_sl) > 1e-9:
            pstate["last_sl"] = pos.sl
            sl_journal_append(symbol, ticket, int(pos.time_update), pos.sl)
        # Kézi BE (a Pozíciók fülről): ha be_done jelölt, de a slot-kezelő még
        # nem tudja, szinkronizáljuk (slot felszabadul, trailing indulhat).
        if pstate.get("be_done") and not is_rf:
            slot_mgr.set_risk_free(ticket)
            is_rf = True

        _is_partial = _preset in (_rr.PRESET_HALVING, _rr.PRESET_SHIELD)

        # Restart-védelem: ha a bot újraindult egy MÁR részlegesen zárt (Felező/
        # Pajzs) pozíció közben, a pstate elveszett → az MT5 history-ból derítsük ki,
        # hogy volt-e már részleges zárás, és NE zárjunk megint (dupla zárás ellen).
        if _is_partial and "rr_reduced" not in pstate:
            if mt5_connector.has_partial_close(ticket):
                pstate["rr_reduced"]  = True
                pstate.setdefault("runner_mode", _spec.get("runner_stop", _rr.RUNNER_TRAILING))
                slot_mgr.set_risk_free(ticket)
                is_rf = True

        if _is_partial:
            # ── Felező/Pajzs: 1R-nél RÉSZLEGES ZÁRÁS (egyszer); a stop TÁVOL marad ──
            _minlot  = pair_cfg.get("min_lot", 0.01)
            _lotstep = pair_cfg.get("lot_step", 0.01)
            if not pstate.get("rr_reduced") and not is_rf:
                one_r = abs(pos.price_open - pstate.get("original_sl", pos.sl))
                reached = (pos.price_current >= pos.price_open + one_r
                           if pos.type == mt5.ORDER_TYPE_BUY
                           else pos.price_current <= pos.price_open - one_r)
                if one_r > 0 and reached:
                    _plan = _rr.plan_at_trigger(_preset, _spec, pos.volume, _minlot, _lotstep)
                    if _plan.close_lot > 0.0 and mt5_connector.close_position_partial(
                            ticket, _plan.close_lot):
                        pstate["rr_reduced"]  = True
                        pstate["runner_mode"] = _plan.runner_stop
                        slot_mgr.set_risk_free(ticket)    # kockázatmentes → slot felszabadul
                        log.info("◐ %s #%d — %s: %.2f lot lezárva 1R-nél, runner=%s",
                                 symbol, ticket, _plan.effective, _plan.close_lot, _plan.runner_stop)
                    elif _plan.close_lot <= 0.0:
                        # túl kicsi a pozíció az osztáshoz → Risky/BE fallback
                        if mt5_connector.move_to_breakeven(ticket):
                            slot_mgr.set_risk_free(ticket)
                            pstate["rr_reduced"]  = True
                            pstate["runner_mode"] = _rr.RUNNER_BREAKEVEN
                            pstate["be_done"]     = True
            # runner BE (ha reduced + runner=breakeven, egyszer)
            if (pstate.get("rr_reduced") and pstate.get("runner_mode") == _rr.RUNNER_BREAKEVEN
                    and not pstate.get("be_done")):
                if mt5_connector.move_to_breakeven(ticket):
                    pstate["be_done"] = True
        else:
            # ── off/risky: költség-tudatos breakeven (VÁLTOZATLAN) ──
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
        # Trailing: off/risky mint eddig; Felező/Pajzsnál CSAK ha a runner-mód trailing.
        _do_trailing = (is_rf and pstate.get("trailing_enabled", True) and
                        (not _is_partial or pstate.get("runner_mode") == _rr.RUNNER_TRAILING))
        if _do_trailing:
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
    from strategy import strategies_for, get_strategy_by_name, default_strategy_name
    primary_name = default_strategy_name(cfg)
    # Stratégia-hatókörű params: az elsődleges stratégia az aktív + egyszeri migráció.
    set_active_strategy(primary_name)
    migrate_flat_layout(primary_name)
    risky_mode.load()                      # induló risky állapot
    last_risky_reload = time.time()
    risky_reload_sec  = cfg.get("trading", {}).get("risky_reload_sec", 3600)

    all_pairs = {s: p for s, p in cfg["pairs"].items() if isinstance(p, dict)}

    # Per-instrumentum ENGEDÉLYEZETT stratégiák (az elsődleges az első). Egy
    # stratégiával (nincs pairs.<sym>.strategies) ez a jelenlegi viselkedés.
    strats_by_symbol = {s: strategies_for(cfg, s) for s in all_pairs}

    # magic → stratégia (recovery-hez). TÖBB stratégiához EGYEDI magic kell; ha
    # ütközés van (két stratégia azonos magic), figyelmeztetünk — broker-szinten
    # nem különíthetők el a pozíciók.
    magic_to_strat: dict = {}
    for _s, _strats in strats_by_symbol.items():
        for _st in _strats:
            _m = _st.magic(cfg)
            if _m in magic_to_strat and magic_to_strat[_m].name != _st.name:
                log.warning("Magic-ütközés: %s és %s ugyanazt a magicet (%d) használja "
                            "— több stratégiához egyedi magic kell (strategy.magic).",
                            magic_to_strat[_m].name, _st.name, _m)
            else:
                magic_to_strat.setdefault(_m, _st)

    # (symbol, strat_name) → LivePairState
    pair_states: dict = {}

    def _make_state(symbol, pair_cfg, strat, is_display):
        _params = load_pair_params(symbol, strat.name)
        if _params is None:
            return None
        return LivePairState(
            symbol=symbol, pair_cfg=pair_cfg, params=_params,
            trading_cfg=trading_cfg, magic=strat.magic(cfg),
            strat_state=strat.new_signal_state(symbol),
            strategy=strat, is_display=is_display)

    # Dashboard + instrument_state inicializálás minden párhoz
    for symbol, pair_cfg in all_pairs.items():
        strats = strats_by_symbol[symbol]
        trained = any(load_pair_params(symbol, st.name) is not None for st in strats)
        dashboard[symbol] = PairDashboardState(
            symbol=symbol,
            enabled=pair_cfg.get("enabled", False),
            trained=trained,
            viz_enabled=pair_cfg.get("viz_enabled", True),   # V mód a config.json-ból
        )
        # Kezdeti állapot: ha enabled és van tanított stratégia → LIVE
        if pair_cfg.get("enabled", False) and trained:
            instrument_state[symbol] = "LIVE"
            for _i, st in enumerate(strats):
                ps = _make_state(symbol, pair_cfg, st, is_display=(_i == 0))
                if ps is not None:
                    pair_states[(symbol, st.name)] = ps
            _n = sum(1 for st in strats if (symbol, st.name) in pair_states)
            log.info("%s — LIVE (%d stratégia, params betöltve)", symbol, _n)
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
    recovered: set = set()   # (symbol, strat_name) — magic alapján stratégiához kötve
    for _m, _st in magic_to_strat.items():
        for _p in get_open_positions(_m):
            slot_mgr.add(_p.ticket)
            if _p.sl and _p.sl != 0.0:
                if _p.type == mt5.ORDER_TYPE_BUY and _p.sl >= _p.price_open:
                    slot_mgr.set_risk_free(_p.ticket)
                elif _p.type == mt5.ORDER_TYPE_SELL and _p.sl <= _p.price_open:
                    slot_mgr.set_risk_free(_p.ticket)
            recovered.add((_p.symbol, _st.name))

    for (_sym, _sname) in recovered:
        _pcfg = all_pairs.get(_sym)
        if not isinstance(_pcfg, dict) or (_sym, _sname) in pair_states:
            continue
        _st = get_strategy_by_name(_sname)
        _primary = strats_by_symbol.get(_sym) or []
        _is_disp = bool(_primary) and _primary[0].name == _sname
        ps = _make_state(_sym, _pcfg, _st, is_display=_is_disp)
        if ps is not None:
            pair_states[(_sym, _sname)] = ps
            instrument_state[_sym] = "LIVE"
            log.info("%s/%s — helyreállítva LIVE-ba (nyitott pozíció a magic alatt)",
                     _sym, _sname)
        else:
            log.warning("%s/%s — nyitott pozíció, de nincs params! Kézi kezelés szükséges.",
                        _sym, _sname)

    if slot_mgr.all_tickets():
        rf = sum(1 for t in slot_mgr.all_tickets() if slot_mgr.is_risk_free(t))
        log.info("Induláskor %d nyitott pozíció helyreállítva (%d kockázatmentes).",
                 len(slot_mgr.all_tickets()), rf)

    log.info("Élő kereskedés indul | %d LIVE pár (%d stratégia-állapot)",
             len({_s for (_s, _n) in pair_states}), len(pair_states))

    while True:
        try:
            balance = mt5_connector.account_balance()

            # Risky állapot óránkénti újraolvasása (külső program írhatja)
            if time.time() - last_risky_reload >= risky_reload_sec:
                risky_mode.load()
                last_risky_reload = time.time()

            for symbol, pair_cfg in all_pairs.items():
                state_now = instrument_state.get(symbol, "STOPPED")
                strats = strats_by_symbol[symbol]

                # Play → LIVE: a hiányzó (symbol, strat) állapotok létrehozása friss
                # params-szal (minden engedélyezett, tanított stratégiához).
                if state_now == "LIVE":
                    for _i, st in enumerate(strats):
                        key = (symbol, st.name)
                        if key not in pair_states:
                            ps = _make_state(symbol, pair_cfg, st, is_display=(_i == 0))
                            if ps is not None:
                                pair_states[key] = ps
                                log.info("%s/%s — Play: LIVE indítva", symbol, st.name)
                    if not any((symbol, st.name) in pair_states for st in strats):
                        instrument_state[symbol] = "STOPPED"
                        log.warning("%s — Play: egyik stratégiához sincs params, STOPPED", symbol)

                # Stop → az összes (symbol, strat) állapot eltávolítása
                elif state_now == "STOPPED":
                    for st in strats:
                        if (symbol, st.name) in pair_states:
                            del pair_states[(symbol, st.name)]
                            log.info("%s/%s — Stop: LIVE leállítva", symbol, st.name)

                # LIVE: feldolgozás stratégiánként (mindegyik a saját magicjével)
                if instrument_state.get(symbol) == "LIVE":
                    for st in strats:
                        key = (symbol, st.name)
                        if key in pair_states:
                            process_pair(pair_states[key], slot_mgr, balance, st)

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
