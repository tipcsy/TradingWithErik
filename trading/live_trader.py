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
from core import run_state
from core import exit_signal
from core import position_build as _position_build
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
    resolve_trade_hours,
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

    # ── Stratégia-specifikus cellák PER STRATÉGIA: {strat_név: {stádium: (szöveg,
    # szín-név)}} ─── Több stratégia futhat egy páron, mindegyiknek SAJÁT oszlopa/
    # köre van. LIVE párnál a MOTOR tölti a saját jelzésállapotából (live_cells);
    # STOPPED/preview párnál a GUI (compute_display). A cells_ts a motor utolsó
    # írásának ideje (bármely stratégia): ha friss, a GUI nem írja felül.
    strategy_cells:   dict = field(default_factory=dict)
    cells_ts:         float = 0.0
    # Mely stratégiák engedélyezettek ezen az instrumentumon (a GUI szürkíti a
    # ki-kapcsoltak köreit). Üres → az elsődleges/aktív stratégia.
    enabled_strategies: list = field(default_factory=list)

    # Piac-előszűrő (market strategy) — a kiválasztott osztályozó neve (vagy None),
    # és az AKTUÁLIS piac-állapot rövid címkéje + szemantikus színe a „Piac" oszlophoz.
    market_strategy:    Optional[str] = None
    market_state_label: str = ""
    market_state_color: str = "muted"

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

# Per-szimbólum POZÍCIÓÉPÍTÉS-futásidejű állapot (a motor tölti, a GUI olvassa a „＋"
# gombhoz és a manuális építéshez). Kulcsonként:
#   {"mode","ready","direction","avg_price","ref_close","next_lot","size_factor"}
# A `ref_close` a legutóbbi ráépítés referencia-záróára (a GUI frissíti add-kor).
build_runtime: dict[str, dict] = {}

# A manuális építés (manual_build) eléréséhez — a run() tölti indításkor.
_run_cfg: dict = {}
_run_slot_mgr = None

# MT5 chart-vizualizáció: engedélyezés + írásgyakoriság (run() tölti a configból),
# és a per-szimbólum utolsó írás ideje (throttle — ne írjunk minden 10 mp-ben mélyet).
VIZ_ENABLED:      bool  = True
VIZ_INTERVAL_SEC: float = 15.0
_viz_last_write:  dict[str, float] = {}
# Per-szimbólum „a következő viz-írás CLEAR-rel kezdjen" kérés — paraméter-váltás
# után (instrument-ablak Mentés) állítjuk, hogy a régi belépő-jelzések garantáltan
# eltűnjenek EGY atomi írásban (a snapshot elé CLEAR sor). A _write_symbol_viz fogyasztja.
_viz_pending_clear: dict[str, bool] = {}


def request_viz_clear(symbol: str) -> None:
    """A KÖVETKEZŐ viz-írás a `symbol`-hoz CLEAR-rel kezdjen (törli a régi chart-
    objektumokat, majd frissen rajzol), és azonnal frissüljön (throttle nullázva).
    Az instrumentum-ablak Mentése hívja, hogy az új paraméterek szerinti beszállási
    jelzések tisztán jelenjenek meg. Biztonságos, ha nem fut a live loop (a flag
    kitart a következő írásig)."""
    _viz_pending_clear[symbol] = True
    _viz_last_write.pop(symbol, None)   # a throttle megkerülése → azonnali újrarajz


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
    # Az első bemelegítés MÉLY M15-ablakból történik (a jelzés-állapotgép a teljes
    # előzménytől függ → egyezzen a vizzel); utána inkrementális, sekély fetch elég.
    signal_warmed: bool = False


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


def apply_no_trade(objects: list, pair_cfg: dict, trade_hours=None) -> list:
    """KERETRENDSZER-szintű no-trade MASZKOLÁS a `trade_hours` alapján. A stratégia
    az órákról nem tud — a KÜLDŐ (keret) alkalmazza a saját szabályát:

      • per-gyertya `BarState`: no-trade órában notrade=1 ÉS dir=0, window=0 →
        a TradeForgeBands al-ablak ott CSAK a szürke sávot mutatja (trend/kék el);
      • belépő-jelölések (`VLine` = m1sig, `Trend` = entry/TP/SL): no-trade órában
        KIMARADNAK — ott a motor úgysem kötne, a jelölés félrevezető lenne.

    A Viz „buta" marad (mindent úgy rajzol, ahogy kap); a szeparáció megmarad: a
    stratégia NYERS jelét a keret a kereskedési-óra szabályával fedi el. Az óra a
    SZERVER/CHART idő UTC epochból — ugyanaz, amivel a live `process_pair` kapuz.

    `trade_hours`: a MÁR feloldott (stratégia-hatókörű) óra-lista; ha None, a régi
    config.json szimbólum-szintű `trade_hours`-ra esik vissza (visszafelé komp.).
    """
    from strategy import visual as viz
    th = trade_hours if trade_hours is not None else pair_cfg.get("trade_hours")
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


def apply_market_state(objects: list, df15, pair_cfg: dict = None) -> list:
    """KERETRENDSZER-szintű PIAC-ÁLLAPOT overlay: minden per-gyertya `BarState`-hez
    beállítja a `market_state` KÓDOT a per-pár KIVÁLASZTOTT piac-stratégia (config
    `pairs.<sym>.market_strategy`) M15-ös besorolásából.

    Csak akkor tölt, ha (1) van kiválasztott piac-stratégia ÉS (2) a `market_viz`
    kérve van — különben a `market_state` a BarState alap **-1** értékén marad, amit
    a TradeForgeBands „NINCS piac-sáv"-ként értelmez (nem rajzol sávot, 3-sávos
    elrendezés). OSZTÁLYOZÓ-FÜGGETLEN: ha más piac-stratégiát választasz, CSAK a
    besorolás (0..8) cserélődik; a mező + a sáv marad."""
    from core import market_strategy as _ms
    pc = pair_cfg or {}
    name = _ms.market_name_of(pc)
    if not name or not pc.get("market_viz", True):
        return objects                      # nincs osztályozó VAGY nem kérik a charton
    if df15 is None or len(df15) < 3:
        return objects
    try:
        from strategy import visual as viz
        s = _ms.classify_series(name, df15)
        code_by_epoch = {int(ts.timestamp()): _ms.code(name, cat)
                         for ts, cat in s.items()}
        for o in objects:
            if isinstance(o, viz.BarState):
                o.market_state = code_by_epoch.get(int(o.t), 0)
    except Exception as e:
        log.debug("piac-állapot overlay hiba: %s", e)
    return objects


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

    # ── Átlagár (null pont) + ráépítés-küszöb — pozícióépítéskor ────────────
    now_int = int(datetime.now(timezone.utc).timestamp())
    # (a) ÁTLAGÁR: ha ≥2 nyitott pozíció van, a volumen-súlyozott átlagár vízszintes
    #     vonala MAGENTA, TÖMÖR + felirat (ide kerülnek a stopok = null pont). A régi
    #     sárga szaggatott nehezen látszott.
    if positions and len(positions) >= 2:
        _avg = _position_build.average_price([(p.price_open, p.volume) for p in positions])
        if _avg > 0:
            objs.append(viz.Trend(
                name="build_avg", t1=int(since_ts), p1=_avg,
                t2=now_int, p2=_avg, color="magenta", width=2, style=0))
            objs.append(viz.Text(
                name="build_avg_lbl", t1=now_int, p1=_avg,
                text=f"Atlagar {_avg:.6g} (null pont - ide a stopok)",
                color="magenta", fontsize=9))
    # (b) RÁÉPÍTÉS-KÜSZÖB: a build_runtime ref_close-a — a KÖVETKEZŐ ráépítés akkor
    #     tüzel, ha egy ZÁRT gyertya E FÖLÉ (BUY) / E ALÁ (SELL) zár. Így LÁTOD, mehet-e
    #     tovább, vagy még várni kell. Zöld (lime) = MEHET (ready); cián = még vár.
    _rt = build_runtime.get(symbol)
    _lvl = _rt.get("next_level") if _rt else None
    if _rt and _lvl and _rt.get("mode") not in (None, "off") and positions:
        _lvl   = float(_lvl)
        _ready = bool(_rt.get("ready"))
        _dir   = _rt.get("direction", "BUY")
        _trig  = _rt.get("trigger", "candle")
        _col   = "lime" if _ready else "cyan"
        objs.append(viz.Trend(
            name="build_ref", t1=int(since_ts), p1=_lvl, t2=now_int, p2=_lvl,
            color=_col, width=1, style=1))
        _side = "fole" if _dir == "BUY" else "ala"
        # A felirat a trigger-mód szerint: gyertyás = ref-küszöb; R-alapú = a következő
        # R-szint (mennyi R-nél jön a következő adalék).
        if _ready:
            _txt = "Raepites: MEHET (trigger atutve)"
        elif _trig == "candle":
            _txt = f"Raepites-kuszob {_lvl:.6g} (e {_side} zarva -> ujabb add)"
        else:
            _rp = _rt.get("r_price") or 0.0
            _ie = _rt.get("initial_entry") or _lvl
            _rmul = (abs(_lvl - _ie) / _rp) if _rp else 0.0
            _txt = f"Kov. raepites: +{_rmul:.2g}R @ {_lvl:.6g}"
        objs.append(viz.Text(
            name="build_ref_lbl", t1=now_int, p1=_lvl, text=_txt,
            color=_col, fontsize=9))
    return objs


def pair_visual_lines(symbol: str, params: dict, strategy, pip_size: float,
                      pair_cfg: dict = None) -> list:
    """Egy stratégia + keretrendszer rajz-objektumainak TAGELT sorai (a stratégia
    nevével — `strategy.visual.tag_line`), hogy több stratégia UGYANABBA a
    szimbólum-fájlba írhasson, az MQL5 indikátor pedig `InpStrategy` szerint szűrjön.
    MÉLY adatablakot tölt (visual_lookback_bars). Üres lista, ha nincs adat."""
    bars = {}
    for tf in strategy.timeframes():
        n = strategy.visual_lookback_bars(params, tf.label)
        if n <= 0:
            continue
        df = get_candles(symbol, mt5_timeframe(tf.minutes), n)
        if df is None or len(df) < 3:
            return []
        bars[tf.label] = df
    if not bars:
        return []
    # A stratégia per-gyertya BarState-jeire a keret RÁMASZKOLJA a no-trade órákat
    # — a STRATÉGIA-hatókörű órákkal (fájl → különben a config.json legacy).
    th = resolve_trade_hours(symbol, strategy.name, (pair_cfg or {}).get("trade_hours"))
    no_trade_set = (set(range(24)) - {int(h) for h in th}) if th is not None else set()
    # pip_size a jövőbeli TP/SL-rajzoláshoz (feltétel 3) — a params nem tartalmazza.
    # A no_trade_hours-t a visual_objects ELŐTT állítjuk be → a kék sáv + belépő-
    # jelölések visszajátszása ugyanúgy RESETEL a szüneteknél, mint a live motor.
    md = MarketData(symbol=symbol, params={**params, "pip_size": pip_size}, bars=bars,
                    no_trade_hours=no_trade_set)
    objects = apply_no_trade(strategy.visual_objects(md), pair_cfg or {}, th)
    # + a GENERIKUS piac-állapot kód a per-gyertya BarState-ekhez (a per-pár
    #   kiválasztott piac-stratégiából; csak ha kérve van a charton).
    objects = apply_market_state(objects, bars.get("M15"), pair_cfg)
    # + a VALÓS kötések nyilai ugyanarra az M1-ablakra (a maszkolás UTÁN).
    m1 = bars.get("M1")
    if m1 is not None and len(m1):
        objects = objects + actual_trade_objects(symbol, int(m1.index[0].timestamp()))
    from strategy.visual import tag_line
    return [tag_line(o.line(), strategy.name) for o in objects]


def write_pair_visuals(symbol: str, params: dict, strategy, pip_size: float,
                       pair_cfg: dict = None):
    """Egy stratégia viz-e a szimbólum-fájlba (tool/kompat — pl. viz_render).
    A több-stratégiás élő út a `run()` per-szimbólum koordinátorán megy át."""
    mt5_visual.write_lines(
        symbol, pair_visual_lines(symbol, params, strategy, pip_size, pair_cfg))


def _write_symbol_viz(symbol, pair_cfg, strats, pair_states):
    """Egy szimbólum chart-vizualizációja: MINDEN élő, engedélyezett stratégia
    rajza EGY `TFV_<symbol>.csv` fájlba, stratégia-taggel (az MQL5 `InpStrategy`
    input szerint szűr → az egyik chart-ablak az A-t, a másik a B-t mutatja).
    Throttle-olva; a viz MEGJELENÍTÉS → kereskedési szünetben is frissül."""
    ds = dashboard.get(symbol)
    if not (VIZ_ENABLED and ds is not None and ds.viz_enabled):
        return
    now = time.time()
    if now - _viz_last_write.get(symbol, 0.0) < VIZ_INTERVAL_SEC:
        return
    _viz_last_write[symbol] = now
    pip_size = pair_cfg.get("pip_size")
    if not pip_size:
        return
    lines = []
    for st in strats:
        key = (symbol, st.name)
        if key not in pair_states:
            continue
        try:
            # A LEGFRISSEBB JSON-paraméter (követi az instrumentum-ablak Mentését).
            vparams = load_pair_params(symbol, st.name) or pair_states[key].params
            lines += pair_visual_lines(symbol, vparams, st, pip_size, pair_cfg)
        except Exception as e:
            log.debug("%s/%s — viz sor hiba: %s", symbol, st.name, e)
    # Paraméter-váltás után egyszeri CLEAR a snapshot elé → a régi (elavult) belépő-
    # jelzések garantáltan eltűnnek, majd az új paraméterekkel frissen rajzol.
    clear_first = _viz_pending_clear.pop(symbol, False)
    try:
        mt5_visual.write_lines(symbol, lines, clear_first=clear_first)
    except Exception as e:
        log.debug("%s — viz írás hiba: %s", symbol, e)


def _apply_be_and_trailing(symbol, pos, ticket, pstate, is_rf, risky,
                           params, pip_size, sym_info, slot_mgr):
    """Egy nyitott (off/risky) pozíció költség-tudatos BREAKEVEN + TRAILING kezelése —
    BAR-FÜGGETLEN, ezért a no-trade (szürke) órákban is futtatható, hogy a már nyitott
    pozíció trailingje/BE-je akkor is dolgozzon. A logika AZONOS a `process_pair` fő
    ágának off/risky BE+trailing részével (csak kiemelve, hogy a szünet-ág is hívhassa);
    a Felező/Pajzs részleges zárás + RUNNER_EXIT (ami bart igényel) marad a fő ágban.
    A `pstate`-et módosítja, MT5-öt hív."""
    # ── Költség-tudatos breakeven ──
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
    # ── Trailing (kockázatmentes után, ha kézzel nincs kikapcsolva) ──
    is_rf = slot_mgr.is_risk_free(ticket)
    if is_rf and pstate.get("trailing_enabled", True):
        point  = sym_info.point if (sym_info and sym_info.point > 0) else pip_size
        digits = sym_info.digits if sym_info else 5
        override_points = pstate.get("trail_points")
        if override_points is not None:
            dist_price = override_points * point
        else:
            base_pips  = params.get("trail_distance_pips", 6)
            dist_price = pip_to_price(base_pips, pip_size) * (0.5 if risky else 1.0)
        min_stop_price = (sym_info.trade_stops_level * point) if sym_info else 0.0
        eff_price = max(dist_price, min_stop_price + point)
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
    #   • fibo   : stop-húzás a belépő→TP táv 61,8%-ánál a fibo_stop_level szintre
    #     (0 = BE); nincs részleges zárás, nincs trailing (a stop ott marad).
    #   • thirds : Harmados (1/3–2/3) — az alap-táv (thirds_base_R×R) megtételekor
    #     a stop az 1/3-ra, a célár érintésekor a 2/3-ra; nincs zárás/trailing.
    #   • shield_fibo : Pajzs↔Fibo auto — pozíciónként dől el (nagy mozgásnál
    #     Fibo, különben Pajzs; ATR vs átlag, big_move_atr_mult).
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

    # A chart-vizualizációt a run() per-szimbólum koordinátora írja (minden
    # engedélyezett stratégiát EGY fájlba, stratégia-taggel) — nem itt, mert egy
    # szimbólumhoz több stratégia is futhat és nem írhatják felül egymás fájlját.

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
    # STRATÉGIA-hatókörű óra-kapu: a stratégia saját `{symbol}_hours.json`-ja (ha
    # van), különben a régi config.json szimbólum-szintű trade_hours (legacy).
    _sn = state.strategy.name if state.strategy else None
    trade_hours = resolve_trade_hours(symbol, _sn, pair_cfg.get("trade_hours"))
    if trade_hours is not None:
        _allowed = {int(h) for h in trade_hours}
    else:
        sess_start = pair_cfg.get("sess_start", 0)
        sess_end   = pair_cfg.get("sess_end", 24)
        _allowed = set(range(int(sess_start), int(sess_end)))
    no_trade_set = set(range(24)) - _allowed        # a jelzés-reset ezekben az órákban
    if hour not in _allowed:
        # No-trade (szürke) óra: ÚJ belépőt NEM nyitunk, de a MÁR NYITOTT pozíciókat
        # tovább kezeljük — automatikus BE + trailing —, hogy a szünet alatt is húzzon
        # a stop (a user kérése: nyitott pozíciót az adott órában is lehessen kezelni).
        # A JELZÉS-ablakot NEM bántjuk (on_bar_close nem fut → befagy a szünetre). A
        # Felező/Pajzs részleges zárás + RUNNER_EXIT (bart igényel) és a Fibo
        # stop-húzás a tradeable ágban fut.
        if _preset in (_rr.PRESET_OFF, _rr.PRESET_RISKY):
            try:
                _sinfo = mt5.symbol_info(symbol)
                for _p in get_open_positions(magic):
                    if _p.symbol != symbol:
                        continue
                    _ps = position_state.setdefault(_p.ticket, {
                        "original_sl": _p.sl, "trailing_enabled": True,
                        "be_done": slot_mgr.is_risk_free(_p.ticket),
                        "trail_points": None, "trail_moved": False})
                    _apply_be_and_trailing(
                        symbol, _p, _p.ticket, _ps, slot_mgr.is_risk_free(_p.ticket),
                        risky, params, pip_size, _sinfo, slot_mgr)
            except Exception as _e:
                log.debug("%s — no-trade pozíció-kezelés hiba: %s", symbol, _e)
        # Ha a stratégia be van kapcsolva rá (`no_trade_resets_signal`), a szünet
        # RESETELJE az M15 jelzést — a következő tradeable ciklusban az on_bar_close
        # MÉLY, hour-aware újra-bemelegítést végez (signal_warmed=False), így a szünet
        # ELŐTTI ablak nem él túl a szüneten. Alapból KI → az ablak túléli a szünetet.
        if params.get("no_trade_resets_signal", False):
            state.signal_warmed = False
            if state.strat_state is not None and hasattr(state.strat_state, "last_m15_time"):
                state.strat_state.last_m15_time = None
        return

    # Napi reset
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.daily_date != today:
        state.daily_date = today
        state.daily_pnl  = 0.0

    # Napi limit — MT5 HISTORY-alapú realizált napi P&L (számla-szintű), NEM a
    # session-lokális state.daily_pnl (az újraindítás után nullázódott, így a
    # kapu „elfelejtette" a veszteséget: a fejléc STOP-ot mutatott, de a motor
    # tovább kereskedett). Az érték a felületről állítható (daily_loss_limit_usd,
    # ha nincs → daily_loss_limit_pct × egyenleg). FONTOS: a limit CSAK az ÚJ
    # belépőt tiltja — a nyitott pozíciók kezelése (BE/trailing/részleges zárás/
    # exit/cost-cut) limit fölött is fut tovább (korábban a korai return azt is
    # leállította volna).
    from trading.backtest import daily_limit_usd as _dlim
    daily_limit = _dlim(trading_cfg, balance)
    _real_daily = mt5_connector.daily_pnl_cached()
    _day_pnl = _real_daily if _real_daily is not None else state.daily_pnl
    daily_limit_hit = (_day_pnl <= -daily_limit)

    # --- Piaci adat a stratégia időkereteire ---
    # Az ELSŐ bemelegítéskor MÉLY ablakot töltünk (signal_warmup_bars — a jelzés-
    # állapotgép a teljes előzménytől függ, hogy a live a vizzel EGYEZŐ ablak-
    # állapotot adjon); utána sekély (warmup_bars) elég, mert az on_bar_close
    # inkrementálisan viszi tovább az állapotot. A KÖTELEZŐ minimum mindig az
    # indikátor-warmup (rövid előzményű szimbólum is működik, a viz is lenient).
    bars = {}
    for tf in strategy.timeframes():
        wu_min = strategy.warmup_bars(params, tf.label)
        wu = (strategy.signal_warmup_bars(params, tf.label)
              if not state.signal_warmed else wu_min)
        df = get_candles(symbol, mt5_timeframe(tf.minutes), wu)
        if df is None or len(df) < wu_min:
            return
        bars[tf.label] = df
    # A no-trade órákat átadjuk a stratégiának → az M15 jelzés-visszajátszás ezekben
    # az órákban RESETEL (a mély warmup is hour-aware, így a szünet előtti ablak nem
    # épül vissza).
    md = MarketData(symbol=symbol, params=params, bars=bars, no_trade_hours=no_trade_set)

    # --- Jelzés a stratégiától (ZÁRT gyertyán, állapottartó) ---
    state.strat_state, signal = strategy.on_bar_close(state.strat_state, md)
    # A mély első bemelegítés megtörtént (az on_bar_close visszajátszotta a mély
    # ablakot) → a következő ciklusoktól elég a sekély fetch.
    state.signal_warmed = True

    # A tábla jelzés-celláit a MOTOR ÉLŐ állapotából töltjük (nem külön
    # rekonstrukcióból) → a kijelzés PONTOSAN azt mutatja, amivel kereskedünk.
    # Több stratégia esetén MINDEGYIK a SAJÁT oszlopát/köreit írja (per-stratégia
    # kulcs), így nem írják felül egymást.
    try:
        cells = strategy.live_cells(state.strat_state, md)
        ds.strategy_cells[strategy.name] = {k: (c.text, c.color)
                                            for k, c in cells.items()}
        ds.cells_ts = time.time()
    except Exception:
        pass

    # --- Pozícióterv (belépés-kapu + méretezés) a STRATÉGIÁTÓL + spread-kapu ──
    # Konvenció: az első deklarált időkeret a "fő" (magasabb). A belépés-szűrőt
    # (volatilitás) ÉS az SL/TP-méretet a STRATÉGIA adja a `bt_entry` hookban a
    # SAJÁT indikátoraiból → a motor stratégia-független (nem ismer 'atr'-t), és
    # a live UGYANAZT a kaput járja, amit a backtest modellez. A stratégia
    # indikátor-sorát a `bt_indicators`-ból vesszük (ugyanaz, amit a backtest lát).
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

    # Cost-cut (idő-stop, tananyag 2.6): ha bekapcsolt, a nyitás után N fő-tf
    # gyertyányi idővel még VESZTESÉGES pozíciót piaci áron zárjuk (kanóc/zaj
    # korai levágása). Bármely presettel kombinálható. Az idő SZERVER-epoch
    # (pos.time és tick.time egyaránt) → nincs időzóna-csúszás.
    _cc_on   = bool(_spec.get("cost_cut"))
    _cc_secs = (int(_spec.get("cost_cut_bars", 12))
                * strategy.timeframes()[0].minutes * 60)

    # Pajzs↔Fibo auto: „nagy mozgás"-e MOST a piac? A generikus (keretrendszer-
    # szintű) ATR-ből (ugyanaz, mint a spread-kapué): aktuális ATR vs a betöltött
    # ablak átlaga. A döntés pozíciónként EGYSZER születik (első kezeléskor,
    # a nyitáshoz közel) és a pstate-ben cache-elődik.
    _big_move_now = False
    if _preset == _rr.PRESET_SHIELD_FIBO:
        try:
            _atr_avg_now = float(atr_ser.iloc[-100:].mean())
            _big_move_now = _rr.big_move(atr_val, _atr_avg_now, _spec)
        except Exception:
            _big_move_now = False

    for pos in symbol_positions:
        ticket = pos.ticket
        pnl    = pos.profit
        is_rf  = slot_mgr.is_risk_free(ticket)

        if _cc_on and pnl < 0 and _tick is not None \
                and (_tick.time - pos.time) >= _cc_secs:
            if mt5_connector.close_position(ticket):
                log.info("✂ %s #%d — Cost-cut: %d fő-gyertya után még veszteséges "
                         "(%.2f$) → korai zárás", symbol, ticket,
                         int(_spec.get("cost_cut_bars", 12)), pnl)
                continue

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

        # Pajzs↔Fibo auto: a HATÁSOS preset pozíciónként dől el (első kezeléskor,
        # a nyitás pillanatához közel) és a pstate-ben cache-elt. Restart után:
        # ha már volt részleges zárás (history), az Pajzs volt → azt visszük tovább.
        _p_eff = _preset
        if _preset == _rr.PRESET_SHIELD_FIBO:
            _m = pstate.get("sf_mode")
            if _m not in (_rr.PRESET_SHIELD, _rr.PRESET_FIBO):
                try:
                    _m = (_rr.PRESET_SHIELD if mt5_connector.has_partial_close(ticket)
                          else (_rr.PRESET_FIBO if _big_move_now else _rr.PRESET_SHIELD))
                except Exception:
                    _m = _rr.PRESET_FIBO if _big_move_now else _rr.PRESET_SHIELD
                pstate["sf_mode"] = _m
                log.info("⇄ %s #%d — Pajzs↔Fibo auto: %s (nagy mozgás: %s)",
                         symbol, ticket, "Fibo" if _m == _rr.PRESET_FIBO else "Pajzs",
                         "igen" if _big_move_now else "nem")
            _p_eff = _m

        _is_partial = _p_eff in (_rr.PRESET_HALVING, _rr.PRESET_SHIELD)
        _is_fibo    = _p_eff == _rr.PRESET_FIBO
        _is_thirds  = _p_eff == _rr.PRESET_THIRDS

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
                    _plan = _rr.plan_at_trigger(_p_eff, _spec, pos.volume, _minlot, _lotstep)
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
            # runner KISZÁLLÁSI JEL: a maradékot (Pajzs/Felező, TP nélkül fut) a
            # kiszállási jelre zárjuk. A „virtuális célár" itt automatikus — a
            # részleges zárás UTÁN kezdjük figyelni —, a stop közben TÁVOL marad.
            if (pstate.get("rr_reduced")
                    and pstate.get("runner_mode") == _rr.RUNNER_EXIT):
                _ex   = _spec.get("exit") or {}
                _exbars = bars.get(_ex.get("timeframe", "M15"))
                _dir  = "BUY" if pos.type == mt5.ORDER_TYPE_BUY else "SELL"
                if exit_signal.exit_triggered(_exbars, _dir, _ex) and \
                        mt5_connector.close_position(ticket):
                    log.info("⎗ %s #%d — runner lezárva KISZÁLLÁSI JELRE (%s/%s)",
                             symbol, ticket, _ex.get("indicator"), _ex.get("timeframe"))
                    continue   # a pozíció zárva → ne kezeljük tovább ebben a körben
        elif _is_fibo:
            # ── Fibo: stop-mozgatás a belépő→TP táv fibo_level (61,8%) pontján ──
            # A trigger ELŐTT a stop TÁVOL marad (nincs BE/trailing — hagyjuk
            # futni); a trigger UTÁN a stop a fibo_stop_level szintre áll
            # (0 = BE) és OTT MARAD. Nincs részleges zárás. Restart-biztos:
            # ha a stop már a cél-szinten (vagy jobb) van, csak megjelöljük.
            if not pstate.get("rr_fibo_done"):
                _trig, _new_stop = _rr.fibo_levels(pos.price_open, pos.tp, _spec)
                if _trig:
                    _is_buy  = pos.type == mt5.ORDER_TYPE_BUY
                    _digits  = sym_info.digits if sym_info else 5
                    _already = (pos.sl > 0 and
                                (pos.sl >= _new_stop if _is_buy else pos.sl <= _new_stop))
                    _reached = (pos.price_current >= _trig if _is_buy
                                else pos.price_current <= _trig)
                    if _already or (_reached and
                                    modify_sl(ticket, round(_new_stop, _digits))):
                        pstate["rr_fibo_done"] = True
                        # BE-n vagy fölötte a stop → kockázatmentes (slot felszabadul)
                        _rf_now = (pos.sl if _already else _new_stop)
                        if ((_is_buy and _rf_now >= pos.price_open) or
                                (not _is_buy and _rf_now <= pos.price_open)):
                            slot_mgr.set_risk_free(ticket)
                            pstate["be_done"] = True
                        if not _already:
                            log.info("𝜑 %s #%d — Fibo: stop a %.5f szintre húzva "
                                     "(trigger: a belépő→TP táv %.1f%%-a)",
                                     symbol, ticket, _new_stop,
                                     float(_spec.get("fibo_level", 0.618)) * 100)
        elif _is_thirds:
            # ── Harmados (1/3–2/3): R-alapú stop-létra, nincs zárás/trailing ──
            # A kezdeti R-t a TP-ből származtatjuk (|TP-open| / tp_rr_ratio) →
            # RESTART-BIZTOS (az eredeti SL a stop-húzás után elveszne). 1. lépcső:
            # az alap-táv megtételekor stop az 1/3-ra (profitban → slot fel).
            # 2. lépcső: a célár érintésekor a 2/3-ra (hard TP-nél ritkán él).
            _ratio = float(params.get("tp_rr_ratio", 0) or 0)
            _risk  = (abs(pos.tp - pos.price_open) / _ratio
                      if (pos.tp and _ratio > 0)
                      else abs(pos.price_open - pstate.get("original_sl", pos.sl)))
            if _risk > 0:
                _is_buy = pos.type == mt5.ORDER_TYPE_BUY
                _trig, _stop1, _stop2 = _rr.thirds_levels(
                    pos.price_open, _risk, _is_buy, _spec)
                _digits = sym_info.digits if sym_info else 5
                if not pstate.get("rr_thirds1"):
                    _already = (pos.sl > 0 and
                                (pos.sl >= _stop1 if _is_buy else pos.sl <= _stop1))
                    _reached = (pos.price_current >= _trig if _is_buy
                                else pos.price_current <= _trig)
                    if _already or (_reached and
                                    modify_sl(ticket, round(_stop1, _digits))):
                        pstate["rr_thirds1"] = True
                        slot_mgr.set_risk_free(ticket)   # a stop profitban → slot fel
                        pstate["be_done"] = True
                        if not _already:
                            log.info("⅓ %s #%d — Harmados: stop a %.5f szintre "
                                     "(az alap-táv 1/3-a bezárva)",
                                     symbol, ticket, _stop1)
                elif not pstate.get("rr_thirds2") and pos.tp:
                    _reached2 = (pos.price_current >= pos.tp if _is_buy
                                 else pos.price_current <= pos.tp)
                    _better = (_stop2 > pos.sl if _is_buy else _stop2 < pos.sl)
                    if _reached2 and _better and \
                            modify_sl(ticket, round(_stop2, _digits)):
                        pstate["rr_thirds2"] = True
                        log.info("⅔ %s #%d — Harmados: célár érintve, stop a "
                                 "%.5f szintre (2/3 bezárva)", symbol, ticket, _stop2)
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
        # Trailing: off/risky mint eddig; Felező/Pajzsnál CSAK ha a runner-mód trailing;
        # Fibónál/Harmadosnál SOHA (a stop a kijelölt szinten marad — tiszta stop-mozgatás).
        _do_trailing = (is_rf and pstate.get("trailing_enabled", True)
                        and not _is_fibo and not _is_thirds and
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

    # ── Pozícióépítés-jelzés (a GUI a „＋" gombhoz olvassa a build_runtime-ból) ──
    # Csak KOCKÁZATMENTES pozíciónál építünk (1. szabály). A ready = a gyertyás
    # építés-jel az elsődleges időkeret ZÁRT gyertyáján. A ref_close-t a GUI frissíti
    # a tényleges ráépítéskor; itt csak megőrizzük / első alkalommal a belépőre állítjuk.
    from core import build_state as _bs, position_build as _pb
    _bmode = _bs.get_mode(symbol)
    _rf_pos = [p for p in symbol_positions if slot_mgr.is_risk_free(p.ticket)]
    if _bmode != _pb.MODE_OFF and _rf_pos:
        _bdir  = "BUY" if _rf_pos[0].type == mt5.ORDER_TYPE_BUY else "SELL"
        _avg   = _pb.average_price([(p.price_open, p.volume) for p in symbol_positions])
        _bcfg  = _bs.get_config(symbol)
        _rt    = build_runtime.setdefault(symbol, {})
        _ref   = _rt.get("ref_close")
        if _ref is None:   # első alkalom → a (legkorábbi) belépő ára a kiindulás
            _ref = (min(p.price_open for p in symbol_positions) if _bdir == "BUY"
                    else max(p.price_open for p in symbol_positions))
        _bbars = bars.get(_bcfg.get("timeframe", "M15"))
        _last_lot = min(p.volume for p in symbol_positions)   # a legkisebb = az utolsó add
        # R-referencia az R-alapú triggerekhez: az INDULÓ (legkorábbi) láb kockázata =
        # |belépő − EREDETI SL| (a pstate őrzi az eredetit, mert a build a stopot az
        # átlagárra húzza). n_add = a KÖVETKEZŐ adalék sorszáma (= a nyitott lábak száma).
        _init  = min(symbol_positions, key=lambda p: p.time)
        _init_entry = float(_init.price_open)
        _init_osl   = position_state.get(_init.ticket, {}).get("original_sl", _init.sl)
        _r_price = abs(_init_entry - float(_init_osl)) if _init_osl else 0.0
        _n_add   = len(symbol_positions)
        _trig    = _bcfg.get("trigger", _pb.TRIGGER_CANDLE)
        # A KÖVETKEZŐ trigger árszintje (a viz + a „mehet-e tovább" ehhez mér):
        # gyertyás → a ref_close; R-alapú → az n_add-adik R-szint.
        _next_level = (_ref if _trig == _pb.TRIGGER_CANDLE
                       else _pb.r_level(_init_entry, _r_price, _bdir, _n_add, _bcfg))
        _rt.update({
            "mode":         _bmode,
            "direction":    _bdir,
            "avg_price":    _avg,
            "ref_close":    _ref,
            "trigger":      _trig,
            "r_price":      _r_price,
            "initial_entry": _init_entry,
            "next_level":   _next_level,
            "size_factor":  _bcfg["size_factor"],
            "ready":        _pb.build_fires(_bbars, _bdir, _bcfg, ref_close=_ref,
                                            initial_entry=_init_entry, r_price=_r_price,
                                            n_add=_n_add),
            "next_lot":     _pb.next_lot(_last_lot, _bcfg["size_factor"],
                                         pair_cfg.get("min_lot", 0.01),
                                         pair_cfg.get("lot_step", 0.01)),
        })
        # ── AUTO mód: a motor magától ráépít a jel-gyertyán — de gyertyánként
        # LEGFELJEBB EGYSZER (last_build_bar őr), hogy egy cikluson belül / azonos
        # gyertyán ne duplázzon. A ref_close-t a gyertya-záróra állítjuk (a doc:
        # a következő ráépítéshez az árnak e gyertya záróján TÚL kell esnie).
        if (_bmode == _pb.MODE_AUTO and _rt.get("ready")
                and _bbars is not None and len(_bbars) >= 2):
            _bar_t = int(_bbars.index[-2].timestamp())
            if _rt.get("last_build_bar") != _bar_t and manual_build(symbol):
                _rt["last_build_bar"] = _bar_t
                _rt["ref_close"] = float(_bbars["close"].iloc[-2])
                _rt["ready"] = False
    else:
        build_runtime.pop(symbol, None)

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
                  f"napi veszteség-limit elérve ({_day_pnl:+.2f}$ ≤ -{daily_limit:.0f}$)"
                  if daily_limit_hit else
                  "nincs érvényes ATR (adathiány)" if atr_val is None else
                  "nincs szabad slot" if not slot_mgr.can_open() else
                  "spread túl nagy (piac-kapu)" if not spread_ok else None)
        if _block:
            log.info("⏭ %s %s jel — belépő KIHAGYVA: %s", symbol, signal, _block)

    if (signal != "NONE" and not already_open and not daily_limit_hit
            and atr_val is not None and slot_mgr.can_open() and spread_ok):
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
            # A belépés-kaput ÉS a méretezést a STRATÉGIA adja (bt_entry:
            # volatilitás-szűrő + SL/TP pip) — UGYANAZ a hook, amit a backtest
            # modellez, így az élő viselkedés egyezik az optimalizált/minősített
            # eredménnyel (atr_min_pct/atr_max_pct per instrumentum). None →
            # kihagyás. Diagnosztika: ha a TISZTA méretező (sl_tp_pips) adott
            # volna tervet, akkor a volatilitás-kapu blokkolt — írjuk ki külön.
            plan = strategy.bt_entry(hi_row, params, pip_size) if hi_row is not None else None
            if plan is None:
                _sizing = (strategy.sl_tp_pips(hi_row, params, pip_size)
                           if hi_row is not None else None)
                if _sizing is not None:
                    log.info("⏭ %s %s jel — belépő KIHAGYVA: volatilitás-kapu "
                             "(ATR a megengedett sávon kívül — atr_min_pct/atr_max_pct).",
                             symbol, signal)
                else:
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

def manual_build(symbol: str) -> bool:
    """Kézi ráépítés (a GUI „＋" gombja hívja). A `build_runtime` alapján nyit egy
    piramidális adalék-pozíciót AZONOS irányba, majd az ÖSSZES azonos-szimbólumú
    stopot az új ÁTLAGÁRRA húzza (1. szabály: kockázatmentesség). Visszaad: True, ha
    a ráépítés megtörtént; False, ha nem alkalmas (nincs jel / pozíció / méret)."""
    rt = build_runtime.get(symbol)
    if not rt or not rt.get("ready"):
        return False
    direction = rt.get("direction")
    add_lot   = float(rt.get("next_lot") or 0.0)
    if direction not in ("BUY", "SELL") or add_lot <= 0:
        return False
    with mt5_connector.MT5_LOCK:
        positions = mt5.positions_get(symbol=symbol)
    if not positions:
        build_runtime.pop(symbol, None)
        return False
    magic = positions[0].magic

    # 1) Az adalék megnyitása (nincs fix TP; az SL-t mindjárt az átlagárra tesszük).
    ticket = open_position(symbol, direction, add_lot, sl=0.0, tp=0.0, magic=magic,
                           comment="build", strategy_name="build")
    if not ticket:
        return False
    if _run_slot_mgr is not None:
        _run_slot_mgr.add(ticket)
        _run_slot_mgr.set_risk_free(ticket)   # a build kockázatmentes (SL az átlagáron)

    # 2) Új átlagár az ÖSSZES (immár bővült) pozícióból + minden stop oda (null pont).
    with mt5_connector.MT5_LOCK:
        positions = mt5.positions_get(symbol=symbol) or positions
        info = mt5.symbol_info(symbol)
    digits = info.digits if info else 5
    avg = round(_position_build.average_price(
        [(p.price_open, p.volume) for p in positions]), digits)
    for p in positions:
        # Minden láb SL-je az átlagárra, ÉS a TP TÖRLÉSE (tp=0): különben az induló láb
        # a saját TP-jén ÖNÁLLÓAN zárna, otthagyva a TP nélküli adalékokat (ez okozta,
        # hogy „lezárta a kezdeti pozíciót, és csak a veszteségesek maradtak"). Így a
        # csomag EGYBEN fut → az átlagár-stopig / kiszállási jelig / kézi zárásig.
        mt5_connector.modify_position_sltp(p.ticket, avg, 0.0)
        if _run_slot_mgr is not None:
            _run_slot_mgr.set_risk_free(p.ticket)
        position_state.setdefault(p.ticket, {})["be_done"] = True

    # 3) Referencia-frissítés: a KÖVETKEZŐ ráépítéshez az árnak túl kell esnie az
    #    aktuális ráépítés árán (a fill ár jó proxy a gyertya-záróra).
    _new = next((p for p in positions if p.ticket == ticket), None)
    if _new is not None:
        rt["ref_close"] = float(_new.price_open)
    rt["ready"] = False
    log.info("➕ %s — kézi ráépítés: +%.2f lot %s | átlagár SL=%.*f | %d pozíció",
             symbol, add_lot, direction, digits, avg, len(positions))
    return True


def run(cfg: dict, slot_mgr: SlotManager):
    global VIZ_ENABLED, VIZ_INTERVAL_SEC, _run_cfg, _run_slot_mgr
    _run_cfg = cfg
    _run_slot_mgr = slot_mgr
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
            enabled_strategies=[st.name for st in strats],
        )
        # Kezdeti állapot (restart-biztos): a KERESKEDÉS-SZÁNDÉK a config.json
        # `run_state`-jéből (per stratégia), legacy fallback a szimbólum-szintű
        # `enabled`-re (= az összes engedélyezett stratégia, mint eddig). Csak a
        # "live" szándékú ÉS tanított stratégiákat indítjuk → újraindítás után a
        # korábban futó párok maguktól folytatják a kereskedést.
        _primary = strats[0].name if strats else None
        _live = set(run_state.live_strategies(cfg, symbol, [st.name for st in strats]))
        startable = [st for st in strats
                     if st.name in _live and load_pair_params(symbol, st.name) is not None]
        if startable:
            instrument_state[symbol] = "LIVE"
            for st in startable:
                ps = _make_state(symbol, pair_cfg, st, is_display=(st.name == _primary))
                if ps is not None:
                    pair_states[(symbol, st.name)] = ps
            log.info("%s — LIVE (%d stratégia, params betöltve)", symbol, len(startable))
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
                    # Chart-viz: minden élő stratégia EGY fájlba (stratégia-taggel).
                    _write_symbol_viz(symbol, pair_cfg, strats, pair_states)

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
