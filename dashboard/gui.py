"""
Élő Dashboard — tkinter GUI (stratégia-független váz)

Fülek:
  [Live Dashboard]      — élő kereskedés táblázat, Play/Stop/OPT gombok
  [Portfólió Backtest]  — eszközválasztás, dátum, equity görbe, eredménytáblázat

A táblázat OSZLOP-VEZÉRELT:
  • A VÁZ adja a fix oszlopokat: Instrumentum, BID, ASK, Vált.%, Spread,
    Pozíció, Napi P&L, Opt státusz, Vezérlés.
  • A STRATÉGIA (strategy.get_strategy) adja a középső oszlopokat és a
    visszaszámlálókat, valamint kiszámítja a megjelenítendő cellákat.
Új stratégia = új modul a `strategy` csomagban; ehhez a fájlhoz nem kell nyúlni.
"""

import json
import threading
import time
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dashboard.theme import (
    BG, BG_HEADER, BG_ROW_ODD, BG_ROW_EVEN, BG_INACTIVE, BG_UNTRAINED,
    BG_OPT_ROW, BG_BT,
    FG_WHITE, FG_GREEN, FG_RED, FG_YELLOW, FG_GRAY, FG_GRAY_DIM, FG_BLUE,
    FG_CYAN, FG_ORANGE,
    BTN_PLAY_BG, BTN_PLAY_FG, BTN_STOP_BG, BTN_STOP_FG, BTN_OPT_BG, BTN_OPT_FG,
    BTN_BT_BG, BTN_BT_FG, BTN_DIS_BG, BTN_DIS_FG,
    CANVAS_BG, CANVAS_LINE, CANVAS_REF,
    color as sem_color,
)
from strategy import get_strategy
from strategy.base import Column
from core import risky_mode


# ---------------------------------------------------------------------------
# Fix (váz-szintű) oszlopok
# ---------------------------------------------------------------------------

# Instrumentum-szintű (stratégia-független) oszlopok — elöl
LEADING_COLUMNS = [
    Column("symbol", "Symbol",  10, "w",      kind="fixed"),
    Column("bid",    "BID",      9, "center", kind="fixed"),
    Column("ask",    "ASK",      9, "center", kind="fixed"),
    Column("change", "Vált.%",   7, "center", kind="fixed"),
    Column("spread", "Spread",   9, "center", kind="fixed"),
]
# Pozíció- és vezérlés-szintű oszlopok — hátul
TRAILING_COLUMNS = [
    Column("position", "Pozíció",    10, "center", kind="fixed"),
    Column("daily",    "Napi P&L",    9, "center", kind="fixed"),
    Column("opt",      "Opt státusz",12, "center", kind="fixed"),
]


def build_columns(strategy) -> list[Column]:
    """A teljes oszloplista: fix elöl + stratégia középen + fix hátul."""
    return LEADING_COLUMNS + list(strategy.columns()) + TRAILING_COLUMNS


# ---------------------------------------------------------------------------
# Cella-formázó segédek (váz-szintű oszlopokhoz)
# ---------------------------------------------------------------------------

def _fmt_price(v: Optional[float], digits: int) -> str:
    if v is None:
        return "—"
    return f"{v:.{digits}f}"


def _tick_color(cur: Optional[float], prev: Optional[float]) -> str:
    """Ár tick-szín: növekvő=zöld, csökkenő=piros, egyenlő/nincs=fehér."""
    if cur is None or prev is None:
        return "neutral"
    if cur > prev:
        return "up"
    if cur < prev:
        return "down"
    return "neutral"


def _fixed_cell(key: str, ds, opt_status: str, inst_state: str) -> tuple[str, str]:
    """Egy fix oszlop (text, szemantikus-szín) értéke a dashboard state-ből."""
    if key == "symbol":
        return ds.symbol, "white"
    if key == "bid":
        return _fmt_price(ds.bid, ds.digits), _tick_color(ds.bid, ds.prev_bid)
    if key == "ask":
        return _fmt_price(ds.ask, ds.digits), _tick_color(ds.ask, ds.prev_ask)
    if key == "change":
        if ds.change_pct is None:
            return "—", "muted"
        col = "up" if ds.change_pct > 0 else "down" if ds.change_pct < 0 else "neutral"
        return f"{ds.change_pct:+.2f}%", col
    if key == "spread":
        sp, sp_max = ds.spread_pts, ds.max_spread_pts
        if sp_max > 0:
            return f"{sp}/{sp_max}", ("red" if sp > sp_max else "green")
        return (f"{sp}" if sp > 0 else "—"), "muted"
    if key == "position":
        if ds.position_pnl is not None:
            txt = f"{ds.position_pnl:+.2f}$"
            if getattr(ds, "pos_count", 0) > 1:
                txt += f" ×{ds.pos_count}"
            if ds.risk_free:
                txt += " ✦"
            return txt, ("green" if ds.position_pnl >= 0 else "red")
        return "—", "muted"
    if key == "daily":
        return f"{ds.daily_pnl:+.2f}$", ("green" if ds.daily_pnl >= 0 else "red")
    if key == "opt":
        txt = opt_status or "—"
        if inst_state in ("OPTIMIZING", "QUEUED"):
            col = "yellow" if inst_state == "OPTIMIZING" else "muted"
        else:
            col = "green" if "Kész" in txt else "muted"
        return txt, col
    return "—", "muted"


# ---------------------------------------------------------------------------
# Live Dashboard — egy sor widgetei (oszlop-vezérelt)
# ---------------------------------------------------------------------------

class PairRow:
    def __init__(self, parent: tk.Frame, symbol: str, row_idx: int, columns: list,
                 on_run, on_opt, on_delete, on_risky, on_name_click, mono_font, small_font):
        self.symbol  = symbol
        self.columns = columns
        self._bg     = BG_ROW_ODD if row_idx % 2 == 0 else BG_ROW_EVEN
        self._mono   = mono_font

        self.frame = tk.Frame(parent, bg=self._bg)
        # Nem csomagoljuk magát — _apply_filter_sort() kezeli

        self.labels: dict[str, tk.Label] = {}
        for col in self.columns:
            lbl = tk.Label(self.frame, text="—", width=col.width, anchor=col.anchor,
                           bg=self._bg, fg=FG_GRAY, font=mono_font, padx=4, pady=3)
            lbl.pack(side="left")
            self.labels[col.key] = lbl

        # A Symbol cellára kattintva → optimalizált paraméterek szerkesztője
        self.labels["symbol"].config(cursor="hand2")
        self.labels["symbol"].bind("<Button-1>", lambda e: on_name_click(symbol))

        # Egy gomb a futtatáshoz (Play↔Stop morph) és egy az OPT-hoz (OPT↔STOP morph)
        self.btn_run = tk.Button(self.frame, text="▶", width=3,
                                 bg=BTN_DIS_BG, fg=BTN_DIS_FG, font=small_font,
                                 relief="flat", command=lambda: on_run(symbol))
        self.btn_run.pack(side="left", padx=1)
        self.btn_risky = tk.Button(self.frame, text="R", width=2,
                                   bg=BTN_DIS_BG, fg=BTN_DIS_FG, font=small_font,
                                   relief="flat", command=lambda: on_risky(symbol))
        self.btn_risky.pack(side="left", padx=1)
        self.btn_opt = tk.Button(self.frame, text="OPT", width=4,
                                 bg=BTN_DIS_BG, fg=BTN_DIS_FG, font=small_font,
                                 relief="flat", command=lambda: on_opt(symbol))
        self.btn_opt.pack(side="left", padx=1)
        self.btn_del = tk.Button(self.frame, text="✕", width=2,
                                 bg=BTN_DIS_BG, fg=BTN_DIS_FG, font=small_font,
                                 relief="flat", command=lambda: on_delete(symbol))
        self.btn_del.pack(side="left", padx=(1, 4))

    def _morph_btn(self, btn, text, enabled, active_bg, active_fg):
        if enabled:
            btn.config(text=text, bg=active_bg, fg=active_fg, state="normal")
        else:
            btn.config(text=text, bg=BTN_DIS_BG, fg=BTN_DIS_FG, state="disabled")

    def _blank_all(self, fg, except_keys=()):
        for col in self.columns:
            if col.key == "symbol" or col.key in except_keys:
                continue
            self.labels[col.key].config(text="—", fg=fg)

    def update(self, ds, inst_state: str, opt_status: str, connected: bool = True):
        trained      = ds.trained
        has_position = ds.position_pnl is not None

        if inst_state == "OPTIMIZING":
            bg = BG_OPT_ROW
        elif not trained:
            bg = BG_UNTRAINED
        elif inst_state == "STOPPED":
            bg = BG_INACTIVE
        else:
            bg = self._bg
        self.frame.config(bg=bg)
        for lbl in self.labels.values():
            lbl.config(bg=bg)

        sym_lbl = self.labels["symbol"]

        # Risky gomb — bármely állapotban kapcsolható; narancs, ha aktív
        if getattr(ds, "risky", False):
            self.btn_risky.config(text="R", bg=FG_ORANGE, fg="#1e1e2e", state="normal")
        else:
            self.btn_risky.config(text="R", bg=BTN_DIS_BG, fg=FG_GRAY, state="normal")

        # ── Offline ───────────────────────────────────────────────────────
        if not connected and inst_state not in ("OPTIMIZING", "QUEUED"):
            sym_lbl.config(text=self.symbol, fg=FG_GRAY_DIM,
                           font=("Courier", 9, "italic"))
            self._blank_all(FG_GRAY_DIM)
            self._morph_btn(self.btn_run, "▶",   False, BTN_PLAY_BG, BTN_PLAY_FG)
            self._morph_btn(self.btn_opt, "OPT", False, BTN_OPT_BG,  BTN_OPT_FG)
            self._morph_btn(self.btn_del, "✕",   False, BG_INACTIVE, FG_RED)
            return

        # ── Optimalizálás / sorban áll ──────────────────────────────────────
        if inst_state in ("OPTIMIZING", "QUEUED"):
            sym_lbl.config(text=self.symbol, fg=FG_YELLOW,
                           font=("Courier", 9, "bold"))
            self._blank_all(FG_GRAY_DIM, except_keys=("opt",))
            txt, col = _fixed_cell("opt", ds, opt_status, inst_state)
            self.labels["opt"].config(text=txt, fg=sem_color(col))
            self._morph_btn(self.btn_run, "▶", False, BTN_PLAY_BG, BTN_PLAY_FG)
            # QUEUED → STOP (sorból törlés); OPTIMIZING (fut) → nem szakítható meg
            if inst_state == "QUEUED":
                self._morph_btn(self.btn_opt, "STOP", True, BTN_STOP_BG, BTN_STOP_FG)
            else:
                self._morph_btn(self.btn_opt, "…", False, BTN_OPT_BG, BTN_OPT_FG)
            self._morph_btn(self.btn_del, "✕", False, BG_INACTIVE, FG_RED)
            return

        # ── LIVE / STOPPED ──────────────────────────────────────────────────
        if inst_state == "LIVE":
            sym_lbl.config(text=self.symbol, fg=FG_WHITE, font=("Courier", 9, "bold"))
        elif trained:
            sym_lbl.config(text=self.symbol, fg=FG_GRAY, font=("Courier", 9, "normal"))
        else:
            sym_lbl.config(text=self.symbol, fg=FG_GRAY_DIM, font=("Courier", 9, "italic"))

        for col in self.columns:
            key = col.key
            if key == "symbol":
                continue
            if col.kind == "fixed":
                txt, c = _fixed_cell(key, ds, opt_status, inst_state)
                self.labels[key].config(text=txt, fg=sem_color(c))
            elif col.kind == "countdown":
                rem = ds.timeframe_remaining.get(col.timeframe_min)
                if rem is None:
                    self.labels[key].config(text="—", fg=FG_GRAY)
                else:
                    self.labels[key].config(text=f"{rem//60}:{rem%60:02d}", fg=FG_GRAY)
            else:  # strategy
                cell = ds.strategy_cells.get(key) if trained else None
                if cell:
                    self.labels[key].config(text=cell[0], fg=sem_color(cell[1]))
                else:
                    self.labels[key].config(text="—", fg=FG_GRAY)

        # Gombok
        if inst_state == "LIVE":
            # Play→Stop morph; nyitott pozícióval nem állítható le
            self._morph_btn(self.btn_run, "■", not has_position, BTN_STOP_BG, BTN_STOP_FG)
            self._morph_btn(self.btn_opt, "OPT", False, BTN_OPT_BG, BTN_OPT_FG)
            self._morph_btn(self.btn_del, "✕",  False, BG_INACTIVE, FG_RED)
        else:  # STOPPED
            self._morph_btn(self.btn_run, "▶",  trained, BTN_PLAY_BG, BTN_PLAY_FG)
            self._morph_btn(self.btn_opt, "OPT", True,   BTN_OPT_BG,  BTN_OPT_FG)
            self._morph_btn(self.btn_del, "✕",   True,   BG_INACTIVE, FG_RED)


# ---------------------------------------------------------------------------
# Live Dashboard — fejléc sor (oszlop-vezérelt, rendezhető)
# ---------------------------------------------------------------------------

class HeaderRow:
    def __init__(self, parent: tk.Frame, columns: list, header_font, small_font,
                 on_col_click=None):
        self.columns = columns
        self.frame = tk.Frame(parent, bg=BG_HEADER)
        self.frame.pack(fill="x", padx=2, pady=(4, 0))
        self._lbls: list[tk.Label] = []
        for i, col in enumerate(columns):
            lbl = tk.Label(
                self.frame, text=col.header, width=col.width, anchor=col.anchor,
                bg=BG_HEADER, fg=FG_BLUE, font=header_font,
                padx=4, pady=3, cursor="hand2",
            )
            if on_col_click:
                lbl.bind("<Button-1>", lambda e, idx=i: on_col_click(idx))
            lbl.pack(side="left")
            self._lbls.append(lbl)
        tk.Label(self.frame, text="Vezérlés", width=16,
                 bg=BG_HEADER, fg=FG_BLUE, font=header_font).pack(side="left")
        tk.Frame(parent, bg=FG_GRAY_DIM, height=1).pack(fill="x", padx=2)

    def set_sort(self, col_idx: Optional[int], direction: int):
        for i, lbl in enumerate(self._lbls):
            col = self.columns[i]
            if i == col_idx and direction != 0:
                arrow = "▲" if direction == 1 else "▼"
                lbl.config(fg=FG_CYAN, text=f"{col.header} {arrow}", width=col.width)
            else:
                lbl.config(fg=FG_BLUE, text=col.header, width=col.width)


# ---------------------------------------------------------------------------
# Optimizer vezérlő — adat-előkészítés háttérSZÁLON, számítás külön PROCESSZBEN
# ---------------------------------------------------------------------------

class _LocalProgress:
    """Tartalék haladásjelző, ha a process-pool nem érhető el (egy folyamatban).
    A .put((symbol, done, total)) hívás közvetlenül a státusz dict-be ír."""
    def __init__(self, status: dict):
        self._status = status

    def put(self, item):
        symbol, done, total = item
        pct = int(done / total * 100) if total else 0
        self._status[symbol] = f"{done}/{total}  {pct}%"


class OptimizerController:
    def __init__(self, cfg: dict, strategy, dashboard_ref: dict,
                 instrument_state: dict, optimizer_status: dict,
                 max_parallel: int = 2):
        self.cfg              = cfg
        self.strategy         = strategy
        self.dashboard_ref    = dashboard_ref
        self.instrument_state = instrument_state
        self.optimizer_status = optimizer_status
        self.max_parallel     = max_parallel
        self._lock            = threading.Lock()
        self._queue: list     = []
        self._running: set    = set()

        # Process-pool + folyamatok közti progress-queue (lazán, háttérben)
        self._pool        = None
        self._manager     = None
        self._progress_q  = None
        self._pool_lock   = threading.Lock()
        self._pool_failed = False
        # Eager létrehozás háttérszálon, hogy az első OPT kattintás se akadjon
        threading.Thread(target=self._ensure_pool, daemon=True,
                         name="OptPoolInit").start()

    # ── Process-pool életciklus ──────────────────────────────────────────
    def _ensure_pool(self):
        with self._pool_lock:
            if self._pool is not None or self._pool_failed:
                return
            try:
                import multiprocessing as mp
                from concurrent.futures import ProcessPoolExecutor
                self._manager    = mp.Manager()
                self._progress_q = self._manager.Queue()
                self._pool       = ProcessPoolExecutor(max_workers=self.max_parallel)
                threading.Thread(target=self._drain_progress, daemon=True,
                                 name="OptProgress").start()
            except Exception:
                import logging as _logging
                _logging.getLogger(__name__).warning(
                    "Process-pool nem hozható létre — szálon belüli tartalék.",
                    exc_info=True)
                self._pool = None
                self._pool_failed = True

    def _drain_progress(self):
        """A gyermekfolyamatok haladását a fő státusz dict-be vezeti."""
        while True:
            try:
                symbol, done, total = self._progress_q.get()
            except Exception:
                break
            if symbol in self._running:
                pct = int(done / total * 100) if total else 0
                self.optimizer_status[symbol] = f"{done}/{total}  {pct}%"

    def shutdown(self):
        try:
            if self._pool is not None:
                self._pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        try:
            if self._manager is not None:
                self._manager.shutdown()
        except Exception:
            pass

    # ── Vezérlés ──────────────────────────────────────────────────────────
    def request_optimize(self, symbol: str):
        with self._lock:
            if self.instrument_state.get(symbol) != "STOPPED":
                return
            if symbol in self._running or symbol in self._queue:
                return
            if len(self._running) < self.max_parallel:
                self._start(symbol)
            else:
                self._queue.append(symbol)
                self.instrument_state[symbol] = "QUEUED"
                self.optimizer_status[symbol] = "Várakozik..."

    def cancel_queued(self, symbol: str):
        """Sorban álló (QUEUED) optimalizálás visszavonása. A MÁR FUTÓ nem
        szakítható meg — azt az időtúllépés-védelem zárja le, ha elakad."""
        with self._lock:
            if symbol in self._queue:
                self._queue.remove(symbol)
                self.instrument_state[symbol] = "STOPPED"
                self.optimizer_status[symbol] = ""

    def _start(self, symbol: str):
        self._running.add(symbol)
        self.instrument_state[symbol] = "OPTIMIZING"
        self.optimizer_status[symbol] = "Indul..."
        threading.Thread(target=self._run_worker, args=(symbol,), daemon=True).start()

    def _run_worker(self, symbol: str):
        """HáttérSZÁL: adat-előkészítés (MT5, IO) → a CPU-nehéz optimalizálás
        külön PROCESSZBE. A fő (UI) szál egyiket sem érinti → nem fagy."""
        try:
            from ml.optimizer import optimize_job, params_file
            from trading.backtest import load_data

            opt_cfg     = self.cfg["optimizer"]
            method      = opt_cfg.get("method", "random")
            max_trials  = opt_cfg.get("max_trials", 500)
            train_start = opt_cfg.get("train_start_date", "2025-01-01")
            test_start  = opt_cfg.get("test_start_date", "2025-10-01")
            initial_bal = self.cfg.get("ml", {}).get("starting_balance_eur", 1000.0)
            trading_cfg = self.cfg["trading"]
            pair_cfg    = self.cfg["pairs"][symbol]
            base_params = self.strategy.base_params(self.cfg)

            # ── Adat előkészítés (MT5_LOCK alatt, háttérszálon) ───────────
            from core.mt5_connector import MT5_LOCK
            from tools.download_history import download_pair, _fill_gap
            from datetime import datetime as _dt, timezone as _tz
            import MetaTrader5 as _mt5_dl

            end_dt = _dt.now(_tz.utc)
            with MT5_LOCK:
                connected = _mt5_dl.initialize()

            if connected:
                for tf in (t.label for t in self.strategy.timeframes()):
                    pq = ROOT / "data" / tf.lower() / f"{symbol}.parquet"
                    if pq.exists():
                        self.optimizer_status[symbol] = f"Gap-fill {tf}..."
                        _fill_gap(pq, symbol, tf, end_dt)
                    else:
                        hs = _dt.strptime(
                            self.cfg["data"].get("history_start_date", "2024-10-01"),
                            "%Y-%m-%d",
                        ).replace(tzinfo=_tz.utc)
                        self.optimizer_status[symbol] = f"Letöltés {tf}..."
                        download_pair(symbol, tf, hs, overwrite=False, end=end_dt)
                self.optimizer_status[symbol] = "Adat kész, optimalizálás..."
            else:
                self.optimizer_status[symbol] = "MT5 offline, meglévő adatok..."

            df_m15, df_m1 = load_data(symbol)
            if df_m15 is None:
                self.optimizer_status[symbol] = "Hiba: nincs adat"
                return

            df_m15 = df_m15[df_m15.index >= train_start].copy()
            df_m1  = df_m1[df_m1.index  >= train_start].copy()

            params_list = self.strategy.param_space(
                self.cfg, base_params, method, max_trials)
            self.optimizer_status[symbol] = f"0/{len(params_list)}  0%"

            # ── Számítás külön PROCESSZBEN (GIL-mentes), tartalék: szálon ──
            self._ensure_pool()
            args = (symbol, df_m15, df_m1, params_list, pair_cfg, trading_cfg,
                    initial_bal, test_start)
            timeout_sec = opt_cfg.get("timeout_sec", 1800)   # beragadás-védelem
            if self._pool is not None:
                from concurrent.futures import TimeoutError as _FutTimeout
                fut = self._pool.submit(optimize_job, *args, self._progress_q)
                try:
                    entry = fut.result(timeout=timeout_sec)
                except _FutTimeout:
                    fut.cancel()
                    self.optimizer_status[symbol] = "Hiba: időtúllépés"
                    return   # a finally visszaállítja STOPPED-ra → UI nem ragad be
            else:
                entry = optimize_job(*args, _LocalProgress(self.optimizer_status))

            if "error" in entry:
                self.optimizer_status[symbol] = f"Hiba: {entry['error']}"
                if entry.get("traceback"):
                    self._log_error(symbol, entry["traceback"])
                return

            full = {
                "symbol":       symbol,
                "optimized_at": datetime.utcnow().isoformat(),
                **entry,
            }
            out = params_file(symbol)
            tmp = out.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(full, f, indent=2, ensure_ascii=False, default=str)
            tmp.replace(out)

            # Sikeres: a pár azonnal "tanított" → Play aktiválható
            ds = self.dashboard_ref.get(symbol)
            if ds is not None:
                ds.trained = True
            self.optimizer_status[symbol] = "Kész ✓"

        except Exception as e:
            import traceback
            self._log_error(symbol, traceback.format_exc())
            self.optimizer_status[symbol] = f"Hiba: {e}"
        finally:
            with self._lock:
                self._running.discard(symbol)
                self.instrument_state[symbol] = "STOPPED"
                self._try_start_next()

    @staticmethod
    def _log_error(symbol: str, tb: str):
        import logging as _logging
        _logging.getLogger(__name__).error("OPT hiba [%s]:\n%s", symbol, tb)
        try:
            with open(ROOT / "data" / "opt_error.log", "a", encoding="utf-8") as _ef:
                _ef.write(f"\n{'='*60}\n{datetime.now()} [{symbol}]\n{tb}\n")
        except Exception:
            pass

    def _try_start_next(self):
        while self._queue and len(self._running) < self.max_parallel:
            nxt = self._queue.pop(0)
            if self.instrument_state.get(nxt) == "QUEUED":
                self._start(nxt)


# ---------------------------------------------------------------------------
# Portfólió Backtest Tab  (változatlan logika)
# ---------------------------------------------------------------------------

class PortfolioBacktestTab:
    def __init__(self, parent: tk.Frame, cfg: dict,
                 mono_font, small_font, header_font):
        self.cfg        = cfg
        self.parent     = parent
        self._thread    = None
        self._stop_flag = threading.Event()
        self._progress  = {
            "running": False, "date": "—", "balance": 0.0,
            "n_open": 0, "n_closed": 0, "pct": 0.0,
            "result": None, "error": None,
        }
        self._equity_pts: list = []   # (date_str, balance)
        self._mono   = mono_font
        self._small  = small_font
        self._header = header_font

        self._build_ui()

    def _build_ui(self):
        p = self.parent
        p.configure(bg=BG_BT)

        top = tk.Frame(p, bg=BG_BT)
        top.pack(fill="x", padx=8, pady=6)

        ctrl = tk.Frame(top, bg=BG_BT)
        ctrl.pack(side="left", fill="y")

        tk.Label(ctrl, text="Instrumentumok (optimalizáltak):",
                 bg=BG_BT, fg=FG_BLUE, font=self._header).grid(
                     row=0, column=0, columnspan=4, sticky="w", pady=(0, 4))

        self._sym_vars: dict = {}
        params_dir = ROOT / "data" / "optimized_params"
        optimized  = sorted([f.stem for f in params_dir.glob("*.json")]) \
                     if params_dir.exists() else []

        if not optimized:
            tk.Label(ctrl, text="(Nincs optimalizált instrumentum)",
                     bg=BG_BT, fg=FG_GRAY, font=self._small).grid(
                         row=1, column=0, columnspan=4, sticky="w")
        else:
            cols = 4
            for i, sym in enumerate(optimized):
                var = tk.BooleanVar(value=True)
                self._sym_vars[sym] = var
                cb = tk.Checkbutton(ctrl, text=sym, variable=var,
                                    bg=BG_BT, fg=FG_WHITE, selectcolor=BG_HEADER,
                                    activebackground=BG_BT, activeforeground=FG_WHITE,
                                    font=self._small)
                cb.grid(row=1 + i // cols, column=i % cols, sticky="w", padx=6)

        n_rows = max(1, (len(optimized) + 3) // 4)
        date_row = n_rows + 2

        tk.Label(ctrl, text="Tól:", bg=BG_BT, fg=FG_GRAY,
                 font=self._small).grid(row=date_row, column=0, sticky="e", pady=6)
        self._entry_from = tk.Entry(ctrl, width=12, bg=BG_HEADER, fg=FG_WHITE,
                                    font=self._small, insertbackground=FG_WHITE)
        self._entry_from.insert(0, self.cfg.get("optimizer", {}).get(
            "test_start_date", "2025-10-01"))
        self._entry_from.grid(row=date_row, column=1, padx=4)

        tk.Label(ctrl, text="Ig:", bg=BG_BT, fg=FG_GRAY,
                 font=self._small).grid(row=date_row, column=2, sticky="e")
        self._entry_to = tk.Entry(ctrl, width=12, bg=BG_HEADER, fg=FG_WHITE,
                                  font=self._small, insertbackground=FG_WHITE)
        self._entry_to.insert(0, datetime.now().strftime("%Y-%m-%d"))
        self._entry_to.grid(row=date_row, column=3, padx=4)

        tk.Label(ctrl, text="Kezdő tőke ($):", bg=BG_BT, fg=FG_GRAY,
                 font=self._small).grid(row=date_row+1, column=0, sticky="e", pady=4)
        self._entry_bal = tk.Entry(ctrl, width=10, bg=BG_HEADER, fg=FG_WHITE,
                                   font=self._small, insertbackground=FG_WHITE)
        self._entry_bal.insert(0, str(int(
            self.cfg.get("ml", {}).get("starting_balance_eur", 1000))))
        self._entry_bal.grid(row=date_row+1, column=1, padx=4)

        btn_row = date_row + 2
        self._btn_start = tk.Button(ctrl, text="▶  Backtest indítása", width=20,
                                    bg=BTN_BT_BG, fg=BTN_BT_FG, font=self._small,
                                    relief="flat", command=self._start_bt)
        self._btn_start.grid(row=btn_row, column=0, columnspan=2, pady=8, sticky="w")

        self._btn_stop_bt = tk.Button(ctrl, text="■  Leállítás", width=12,
                                      bg=BTN_DIS_BG, fg=BTN_DIS_FG, font=self._small,
                                      relief="flat", command=self._stop_bt,
                                      state="disabled")
        self._btn_stop_bt.grid(row=btn_row, column=2, columnspan=2, pady=8, sticky="w")

        right = tk.Frame(top, bg=BG_BT)
        right.pack(side="left", fill="both", expand=True, padx=(20, 0))

        prog_frame = tk.Frame(right, bg=BG_BT)
        prog_frame.pack(fill="x")

        self._lbl_status  = tk.Label(prog_frame, text="Kész.", bg=BG_BT,
                                     fg=FG_GRAY, font=self._small)
        self._lbl_status.grid(row=0, column=0, sticky="w")
        self._lbl_date    = tk.Label(prog_frame, text="Dátum: —", bg=BG_BT,
                                     fg=FG_WHITE, font=self._mono)
        self._lbl_date.grid(row=1, column=0, sticky="w")
        self._lbl_bal     = tk.Label(prog_frame, text="Egyenleg: —", bg=BG_BT,
                                     fg=FG_WHITE, font=self._mono)
        self._lbl_bal.grid(row=1, column=1, sticky="w", padx=16)
        self._lbl_pnl     = tk.Label(prog_frame, text="P&L: —", bg=BG_BT,
                                     fg=FG_WHITE, font=self._mono)
        self._lbl_pnl.grid(row=1, column=2, sticky="w", padx=8)
        self._lbl_trades  = tk.Label(prog_frame, text="Lezárt: 0  Nyitott: 0",
                                     bg=BG_BT, fg=FG_GRAY, font=self._small)
        self._lbl_trades.grid(row=2, column=0, columnspan=2, sticky="w")

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("BT.Horizontal.TProgressbar",
                        troughcolor=BG_HEADER, background=BTN_BT_BG, thickness=8)
        self._progressbar = ttk.Progressbar(right, style="BT.Horizontal.TProgressbar",
                                            orient="horizontal", length=400,
                                            mode="determinate", maximum=100)
        self._progressbar.pack(fill="x", pady=(4, 6))

        tk.Label(right, text="Equity görbe:", bg=BG_BT, fg=FG_GRAY,
                 font=self._small).pack(anchor="w")
        self._canvas = tk.Canvas(right, height=140, bg=CANVAS_BG, highlightthickness=0)
        self._canvas.pack(fill="x", pady=(0, 6))

        tk.Frame(p, bg=FG_GRAY_DIM, height=1).pack(fill="x", padx=4, pady=2)

        tk.Label(p, text="Eredmények:", bg=BG_BT, fg=FG_BLUE,
                 font=self._header).pack(anchor="w", padx=8, pady=(4, 0))

        res_frame = tk.Frame(p, bg=BG_BT)
        res_frame.pack(fill="both", expand=True, padx=8, pady=4)

        res_header = tk.Frame(res_frame, bg=BG_HEADER)
        res_header.pack(fill="x")
        for col, w in [("Pár", 10), ("Trade", 6), ("Win%", 7),
                       ("P&L$", 9), ("MaxDD%", 7), ("PF", 6), ("Végegyenleg", 12)]:
            tk.Label(res_header, text=col, width=w, anchor="center",
                     bg=BG_HEADER, fg=FG_BLUE, font=self._small,
                     padx=4, pady=3).pack(side="left")

        tk.Frame(res_frame, bg=FG_GRAY_DIM, height=1).pack(fill="x")

        self._res_rows_frame = tk.Frame(res_frame, bg=BG_BT)
        self._res_rows_frame.pack(fill="both", expand=True)

        self._lbl_res_total = tk.Label(p, text="", bg=BG_BT,
                                       fg=FG_YELLOW, font=self._mono)
        self._lbl_res_total.pack(anchor="w", padx=8, pady=4)

    def _start_bt(self):
        if self._thread and self._thread.is_alive():
            return
        symbols = [s for s, v in self._sym_vars.items() if v.get()]
        if not symbols:
            self._lbl_status.config(text="Válassz legalább egy instrumentumot!", fg=FG_RED)
            return
        date_from = self._entry_from.get().strip()
        date_to   = self._entry_to.get().strip()
        try:
            init_bal = float(self._entry_bal.get().strip())
        except ValueError:
            init_bal = 1000.0

        self._stop_flag.clear()
        self._equity_pts = []
        self._clear_results()
        self._draw_equity([])

        self._btn_start.config(state="disabled", bg=BTN_DIS_BG, fg=BTN_DIS_FG)
        self._btn_stop_bt.config(state="normal", bg=BTN_STOP_BG, fg=BTN_STOP_FG)
        self._progressbar["value"] = 0
        self._lbl_status.config(text=f"Fut... ({len(symbols)} pár)", fg=FG_YELLOW)

        self._thread = threading.Thread(
            target=self._run_thread,
            args=(symbols, date_from, date_to, init_bal),
            daemon=True,
        )
        self._thread.start()
        self.parent.after(200, self._poll_progress)

    def _stop_bt(self):
        self._stop_flag.set()
        self._lbl_status.config(text="Leállítás...", fg=FG_ORANGE)

    def _run_thread(self, symbols, date_from, date_to, init_bal):
        from trading.backtest import run_portfolio_backtest, _save_backtest_results

        def on_progress(date_str, balance, n_open, n_closed, pct):
            self._progress.update({
                "running": True, "date": date_str, "balance": balance,
                "n_open": n_open, "n_closed": n_closed, "pct": pct,
            })
            self._equity_pts.append((date_str, balance))

        try:
            result = run_portfolio_backtest(
                self.cfg, symbols, date_from, date_to,
                initial_balance=init_bal,
                progress_callback=on_progress,
                stop_flag=self._stop_flag,
            )
            if result.get("trades"):
                _save_backtest_results(
                    result["trades"],
                    list(result.get("per_pair", {}).values()),
                    init_bal, date_from,
                )
            self._progress["result"] = result
            self._progress["running"] = False
        except Exception as e:
            self._progress["error"]   = str(e)
            self._progress["running"] = False

    def _poll_progress(self):
        prog = self._progress
        if prog.get("running") or (self._thread and self._thread.is_alive()):
            date_str  = prog.get("date", "—")
            balance   = prog.get("balance", 0.0)
            init_bal  = float(self._entry_bal.get().strip() or 1000)
            pnl       = balance - init_bal
            pnl_pct   = pnl / init_bal * 100 if init_bal else 0
            n_open    = prog.get("n_open", 0)
            n_closed  = prog.get("n_closed", 0)
            pct       = prog.get("pct", 0.0)

            self._lbl_date.config(text=f"Dátum: {date_str}")
            self._lbl_bal.config(text=f"Egyenleg: ${balance:,.2f}")
            pnl_fg = FG_GREEN if pnl >= 0 else FG_RED
            self._lbl_pnl.config(text=f"P&L: {pnl:+.2f}$ ({pnl_pct:+.1f}%)", fg=pnl_fg)
            self._lbl_trades.config(text=f"Lezárt: {n_closed}   Nyitott: {n_open}")
            self._progressbar["value"] = pct

            self._draw_equity(self._equity_pts, init_bal)
            self.parent.after(300, self._poll_progress)
        else:
            self._progressbar["value"] = 100
            self._btn_start.config(state="normal", bg=BTN_BT_BG, fg=BTN_BT_FG)
            self._btn_stop_bt.config(state="disabled", bg=BTN_DIS_BG, fg=BTN_DIS_FG)

            err = prog.get("error")
            result = prog.get("result")
            if err:
                self._lbl_status.config(text=f"Hiba: {err}", fg=FG_RED)
            elif result:
                n = len(result.get("trades", []))
                init_bal = result.get("initial_balance", 1000)
                final    = result.get("final_balance", init_bal)
                pnl      = final - init_bal
                self._lbl_status.config(
                    text=f"Kész! {n} trade | P&L: {pnl:+.2f}$ ({pnl/init_bal*100:+.1f}%)",
                    fg=FG_GREEN if pnl >= 0 else FG_RED)
                self._show_results(result)
                self._draw_equity(result.get("equity_curve", []), init_bal)
            else:
                self._lbl_status.config(text="Leállítva.", fg=FG_GRAY)

    def _draw_equity(self, points: list, init_bal: float = 1000.0):
        c = self._canvas
        c.delete("all")
        w = c.winfo_width() or 500
        h = c.winfo_height() or 140
        pad = 8
        if not points or len(points) < 2:
            c.create_text(w // 2, h // 2, text="Nincs adat", fill=FG_GRAY,
                          font=("Courier", 9))
            return
        balances = [b for _, b in points]
        mn = min(balances + [init_bal])
        mx = max(balances + [init_bal])
        rng = mx - mn or 1

        def px(i):
            return pad + (i / (len(points) - 1)) * (w - 2 * pad)
        def py(b):
            return h - pad - ((b - mn) / rng) * (h - 2 * pad)

        ref_y = py(init_bal)
        c.create_line(pad, ref_y, w - pad, ref_y, fill=CANVAS_REF, dash=(4, 4), width=1)

        coords = []
        for i, (_, b) in enumerate(points):
            coords += [px(i), py(b)]
        if len(coords) >= 4:
            final_bal = balances[-1]
            col = CANVAS_LINE if final_bal >= init_bal else FG_RED
            c.create_line(*coords, fill=col, width=2, smooth=True)

        c.create_text(pad + 2, h - pad - 2, text=f"${mn:.0f}", fill=FG_GRAY,
                      font=("Courier", 7), anchor="sw")
        c.create_text(pad + 2, pad + 2, text=f"${mx:.0f}", fill=FG_GRAY,
                      font=("Courier", 7), anchor="nw")
        if points:
            c.create_text(w - pad, h - pad - 2, text=str(points[-1][0])[:7],
                          fill=FG_GRAY, font=("Courier", 7), anchor="se")
            c.create_text(pad, h - pad - 2, text=str(points[0][0])[:7],
                          fill=FG_GRAY, font=("Courier", 7), anchor="sw")

    def _clear_results(self):
        for w in self._res_rows_frame.winfo_children():
            w.destroy()
        self._lbl_res_total.config(text="")

    def _show_results(self, result: dict):
        self._clear_results()
        per_pair  = result.get("per_pair", {})
        init_bal  = result.get("initial_balance", 1000.0)
        final_bal = result.get("final_balance", init_bal)

        all_trades = result.get("trades", [])
        from collections import defaultdict
        by_sym = defaultdict(list)
        for t in all_trades:
            by_sym[t.symbol].append(t)

        row_idx = 0
        for sym in sorted(per_pair, key=lambda s: -per_pair[s].get("total_pnl", 0)):
            s   = per_pair[sym]
            pnl = s.get("total_pnl", 0)
            pf  = s.get("profit_factor", 0)
            pf_str = f"{min(pf, 99):.2f}" if pf != float("inf") else "∞"
            sym_final = init_bal + pnl

            bg = BG_ROW_ODD if row_idx % 2 == 0 else BG_ROW_EVEN
            fr = tk.Frame(self._res_rows_frame, bg=bg)
            fr.pack(fill="x")
            vals = [
                (sym,                            10, FG_WHITE),
                (str(s.get("trades", 0)),         6, FG_WHITE),
                (f"{s.get('win_rate',0):.0%}",    7, FG_GREEN if s.get('win_rate',0) >= 0.5 else FG_RED),
                (f"{pnl:+.2f}",                   9, FG_GREEN if pnl >= 0 else FG_RED),
                (f"{s.get('max_drawdown',0)*100:.1f}%", 7, FG_YELLOW),
                (pf_str,                           6, FG_WHITE),
                (f"${sym_final:.0f}",             12, FG_GREEN if sym_final >= init_bal else FG_RED),
            ]
            for txt, w, fg in vals:
                tk.Label(fr, text=txt, width=w, anchor="center",
                         bg=bg, fg=fg, font=self._small, padx=4, pady=2).pack(side="left")
            row_idx += 1

        total_pnl = final_bal - init_bal
        n_all  = len(all_trades)
        n_wins = sum(1 for t in all_trades if t.pnl_usd > 0)
        wr_all = n_wins / max(n_all, 1)

        eq = init_bal; peak = eq; mdd = 0.0
        for t in sorted(all_trades, key=lambda x: x.close_time or x.open_time):
            eq += t.pnl_usd
            if eq > peak: peak = eq
            dd = (peak - eq) / peak * 100
            if dd > mdd: mdd = dd

        win_sum  = sum(t.pnl_usd for t in all_trades if t.pnl_usd > 0)
        loss_sum = sum(t.pnl_usd for t in all_trades if t.pnl_usd < 0)
        pf_all   = abs(win_sum / loss_sum) if loss_sum != 0 else float("inf")
        pf_str   = f"{min(pf_all, 99):.2f}" if pf_all != float("inf") else "∞"

        self._lbl_res_total.config(
            text=f"ÖSSZESEN  |  Trade: {n_all}  |  Win: {wr_all:.0%}  |  "
                 f"P&L: {total_pnl:+.2f}$  |  MaxDD: {mdd:.1f}%  |  "
                 f"PF: {pf_str}  |  Végegyenleg: ${final_bal:.0f}",
            fg=FG_GREEN if total_pnl >= 0 else FG_RED,
        )


# ---------------------------------------------------------------------------
# Pozíciók fül — nyitott pozíciók kezelése
# ---------------------------------------------------------------------------

POSITION_COLUMNS = [
    ("symbol",  "Symbol", 10, "w"),
    ("type",    "Irány",   6, "center"),
    ("volume",  "Lot",     6, "center"),
    ("open",    "Nyitó",  10, "center"),
    ("current", "Akt.",   10, "center"),
    ("sl",      "SL",     10, "center"),
    ("tp",      "TP",     10, "center"),
    ("orig_sl", "Er. SL", 10, "center"),
    ("pnl",     "P&L",     9, "center"),
]


class PositionRow:
    def __init__(self, parent, ticket, mono_font, small_font,
                 on_be, on_trail, on_panic):
        self.ticket = ticket
        self.frame = tk.Frame(parent, bg=BG_ROW_EVEN)
        self.labels = {}
        for key, hdr, w, anchor in POSITION_COLUMNS:
            lbl = tk.Label(self.frame, text="—", width=w, anchor=anchor,
                           bg=BG_ROW_EVEN, fg=FG_WHITE, font=mono_font, padx=4, pady=2)
            lbl.pack(side="left")
            self.labels[key] = lbl
        self.btn_be = tk.Button(self.frame, text="BE", width=4, font=small_font,
                                relief="flat", bg=BTN_OPT_BG, fg="#ffffff",
                                command=lambda: on_be(ticket))
        self.btn_be.pack(side="left", padx=1)
        self.btn_trail = tk.Button(self.frame, text="Trail", width=5, font=small_font,
                                   relief="flat", bg=BTN_DIS_BG, fg=FG_GRAY,
                                   command=lambda: on_trail(ticket))
        self.btn_trail.pack(side="left", padx=1)
        self.btn_panic = tk.Button(self.frame, text="Zár", width=4, font=small_font,
                                   relief="flat", bg=BTN_STOP_BG, fg="#ffffff",
                                   command=lambda: on_panic(ticket))
        self.btn_panic.pack(side="left", padx=(1, 4))

    def update(self, pos, pstate, digits):
        self.labels["symbol"].config(text=pos["symbol"])
        t = pos["type"]
        self.labels["type"].config(text=t, fg=FG_GREEN if t == "BUY" else FG_RED)
        self.labels["volume"].config(text=f'{pos["volume"]:.2f}', fg=FG_WHITE)
        self.labels["open"].config(text=_fmt_price(pos["price_open"], digits), fg=FG_GRAY)
        self.labels["current"].config(text=_fmt_price(pos["price_current"], digits), fg=FG_WHITE)

        sl, tp = pos["sl"], pos["tp"]
        orig = pstate.get("original_sl", sl) if pstate else sl
        be_done = bool(pstate and pstate.get("be_done"))
        moved = bool(sl and orig and abs(sl - orig) > 1e-9)
        self.labels["sl"].config(text=_fmt_price(sl, digits) if sl else "—",
                                 fg=FG_CYAN if be_done else FG_WHITE)
        self.labels["tp"].config(text=_fmt_price(tp, digits) if tp else "—", fg=FG_GRAY)
        # Eredeti SL: fehér, de ha a trailing már elmozdította → szürke
        self.labels["orig_sl"].config(text=_fmt_price(orig, digits) if orig else "—",
                                      fg=FG_GRAY if moved else FG_WHITE)
        pnl = pos["profit"]
        self.labels["pnl"].config(text=f"{pnl:+.2f}$", fg=FG_GREEN if pnl >= 0 else FG_RED)

        # Gombok állapota (aktív-e?)
        self.btn_be.config(text="BE ✓" if be_done else "BE",
                           bg=BTN_PLAY_BG if be_done else BTN_OPT_BG)
        trail_on = bool(pstate.get("trailing_enabled", True)) if pstate else True
        self.btn_trail.config(bg=FG_GREEN if trail_on else BTN_DIS_BG,
                              fg="#1e1e2e" if trail_on else FG_GRAY)


class PositionsTab:
    def __init__(self, parent, cfg, mono_font, small_font, header_font,
                 positions_provider, pos_state, digits_provider,
                 on_be, on_trail, on_panic, on_close_all):
        self.parent = parent
        self.cfg = cfg
        self._mono, self._small, self._header = mono_font, small_font, header_font
        self._positions_provider = positions_provider
        self._pos_state = pos_state
        self._digits_provider = digits_provider
        self._on_be, self._on_trail, self._on_panic = on_be, on_trail, on_panic
        self._on_close_all = on_close_all
        self._rows: dict[int, PositionRow] = {}
        self._build_ui()

    def _build_ui(self):
        p = self.parent
        p.configure(bg=BG)
        top = tk.Frame(p, bg=BG, pady=4)
        top.pack(fill="x", padx=8)
        tk.Button(top, text="⚠  ÖSSZES ZÁRÁSA", font=self._small,
                  bg=BTN_STOP_BG, fg="#ffffff", relief="flat", cursor="hand2",
                  command=self._on_close_all).pack(side="left")
        self._lbl_total = tk.Label(top, text="Összes P&L: —", bg=BG,
                                   fg=FG_WHITE, font=self._header)
        self._lbl_total.pack(side="right", padx=8)

        self._lbl_breakdown = tk.Label(p, text="", bg=BG, fg=FG_GRAY,
                                       font=self._small, anchor="w", justify="left")
        self._lbl_breakdown.pack(fill="x", padx=10, pady=(0, 4))

        # Fejléc
        hdr = tk.Frame(p, bg=BG_HEADER)
        hdr.pack(fill="x", padx=2)
        for key, label, w, anchor in POSITION_COLUMNS:
            tk.Label(hdr, text=label, width=w, anchor=anchor, bg=BG_HEADER,
                     fg=FG_BLUE, font=self._header, padx=4, pady=3).pack(side="left")
        tk.Label(hdr, text="Vezérlés", width=16, bg=BG_HEADER, fg=FG_BLUE,
                 font=self._header).pack(side="left")
        tk.Frame(p, bg=FG_GRAY_DIM, height=1).pack(fill="x", padx=2)

        self._rows_frame = tk.Frame(p, bg=BG)
        self._rows_frame.pack(fill="both", expand=True, padx=2)

    def refresh(self):
        positions = self._positions_provider() or []
        seen = set()
        for pos in positions:
            tid = pos["ticket"]
            seen.add(tid)
            row = self._rows.get(tid)
            if row is None:
                row = PositionRow(self._rows_frame, tid, self._mono, self._small,
                                  self._on_be, self._on_trail, self._on_panic)
                self._rows[tid] = row
            row.update(pos, self._pos_state.get(tid), self._digits_provider(pos["symbol"]))

        for tid in list(self._rows):
            if tid not in seen:
                self._rows[tid].frame.destroy()
                del self._rows[tid]

        # Rendezett újracsomagolás (szimbólum szerint)
        for r in self._rows.values():
            r.frame.pack_forget()
        for pos in sorted(positions, key=lambda x: (x["symbol"], x["ticket"])):
            self._rows[pos["ticket"]].frame.pack(fill="x", padx=2)

        # Összesítés + instrumentumonkénti bontás
        total = sum(p["profit"] for p in positions)
        by_sym: dict[str, list] = {}
        for p in positions:
            a = by_sym.setdefault(p["symbol"], [0.0, 0])
            a[0] += p["profit"]
            a[1] += 1
        self._lbl_total.config(
            text=f"Összes P&L: {total:+.2f}$   |   {len(positions)} pozíció",
            fg=FG_GREEN if total >= 0 else FG_RED)
        if by_sym:
            parts = [f"{s}: {v[0]:+.2f}$ ({v[1]})" for s, v in sorted(by_sym.items())]
            self._lbl_breakdown.config(text="   |   ".join(parts), fg=FG_GRAY)
        else:
            self._lbl_breakdown.config(text="Nincs nyitott pozíció.", fg=FG_GRAY)


# ---------------------------------------------------------------------------
# Fő Dashboard ablak
# ---------------------------------------------------------------------------

class DashboardWindow:
    def __init__(self, cfg: dict, dashboard_ref: dict,
                 instrument_state: dict, optimizer_status: dict,
                 on_play_pair, on_stop_pair, strategy=None):
        self.cfg              = cfg
        self.dashboard_ref    = dashboard_ref
        self.instrument_state = instrument_state
        self.optimizer_status = optimizer_status
        self._on_play         = on_play_pair
        self._on_stop         = on_stop_pair
        self.strategy         = strategy or get_strategy(cfg)
        self._columns         = build_columns(self.strategy)

        # Frissítési ütemezés (config-vezérelt)
        dash_cfg = cfg.get("dashboard", {})
        self._price_refresh_sec = dash_cfg.get("price_refresh_sec", 3)   # ár MINDEN párra
        self._fast_refresh_sec  = dash_cfg.get("live_refresh_sec", 7)    # indikátor: LIVE
        self._all_refresh_sec   = dash_cfg.get("all_refresh_sec", 30)    # indikátor: mind

        max_par = cfg.get("optimizer", {}).get("max_parallel_optimizers", 2)
        self._opt_ctrl = OptimizerController(
            cfg, self.strategy, dashboard_ref,
            instrument_state, optimizer_status, max_parallel=max_par)

        self.root = tk.Tk()
        self.root.title("MT5 Erik — Live Dashboard")
        self.root.configure(bg=BG)
        self.root.resizable(True, True)

        mono_font   = tkfont.Font(family="Courier New", size=9)
        header_font = tkfont.Font(family="Courier New", size=9, weight="bold")
        small_font  = tkfont.Font(family="Courier New", size=8)
        title_font  = tkfont.Font(family="Courier New", size=10, weight="bold")
        info_font   = tkfont.Font(family="Courier New", size=9)

        # ── Globális fejléc ─────────────────────────────────────────────
        top_bar = tk.Frame(self.root, bg=BG_HEADER, pady=5)
        top_bar.pack(fill="x", padx=4, pady=(4, 0))
        tk.Label(top_bar, text="MT5 Erik — Dashboard",
                 bg=BG_HEADER, fg=FG_BLUE, font=title_font).pack(side="left", padx=10)
        self.lbl_time = tk.Label(top_bar, text="", bg=BG_HEADER, fg=FG_GRAY, font=info_font)
        self.lbl_time.pack(side="right", padx=10)
        self._btn_connect = tk.Button(
            top_bar, text="⟳  Kapcsolódás", font=small_font,
            bg=BTN_OPT_BG, fg=BTN_OPT_FG, relief="flat", command=self._handle_connect)
        self._btn_connect.pack(side="right", padx=6)
        self._btn_connect.pack_forget()
        self.lbl_conn = tk.Label(top_bar, text="● Offline", bg=BG_HEADER,
                                 fg=FG_RED, font=info_font)
        self.lbl_conn.pack(side="right", padx=(0, 4))
        self.lbl_account = tk.Label(top_bar, text="", bg=BG_HEADER, fg=FG_GRAY, font=info_font)
        self.lbl_account.pack(side="right", padx=10)

        info_bar = tk.Frame(self.root, bg=BG_HEADER, pady=2)
        info_bar.pack(fill="x", padx=4)
        self.lbl_balance = tk.Label(info_bar, text="Egyenleg: —",
                                    bg=BG_HEADER, fg=FG_WHITE, font=info_font)
        self.lbl_balance.pack(side="left", padx=10)
        self.lbl_daily   = tk.Label(info_bar, text="Napi P&L: —",
                                    bg=BG_HEADER, fg=FG_WHITE, font=info_font)
        self.lbl_daily.pack(side="left", padx=10)
        self.lbl_slots   = tk.Label(info_bar, text="Szabad slotok: —/—",
                                    bg=BG_HEADER, fg=FG_WHITE, font=info_font)
        self.lbl_slots.pack(side="left", padx=10)
        self.lbl_limit   = tk.Label(info_bar, text="Napi limit: OK",
                                    bg=BG_HEADER, fg=FG_GREEN, font=info_font)
        self.lbl_limit.pack(side="left", padx=10)

        tk.Frame(self.root, bg=FG_GRAY_DIM, height=1).pack(fill="x", pady=2)

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=BG_HEADER, foreground=FG_GRAY,
                        padding=[12, 4], font=("Courier New", 9))
        style.map("TNotebook.Tab", background=[("selected", BG)],
                  foreground=[("selected", FG_BLUE)])

        self._notebook = ttk.Notebook(self.root)
        self._notebook.pack(fill="both", expand=True, padx=2)

        live_frame = tk.Frame(self._notebook, bg=BG)
        self._notebook.add(live_frame, text="  Live Dashboard  ")
        self._build_live_tab(live_frame, mono_font, header_font, small_font)

        pos_frame = tk.Frame(self._notebook, bg=BG)
        self._notebook.add(pos_frame, text="  Pozíciók  ")
        from trading.live_trader import position_state as _pos_state
        self._pos_tab = PositionsTab(
            pos_frame, cfg, mono_font, small_font, header_font,
            positions_provider=lambda: getattr(self, "_mt5_cache", {}).get("positions_detail", []),
            pos_state=_pos_state,
            digits_provider=lambda sym: getattr(self.dashboard_ref.get(sym), "digits", 5),
            on_be=self._pos_be, on_trail=self._pos_trail,
            on_panic=self._pos_panic, on_close_all=self._pos_close_all)

        bt_frame = tk.Frame(self._notebook, bg=BG_BT)
        self._notebook.add(bt_frame, text="  Portfólió Backtest  ")
        self._bt_tab = PortfolioBacktestTab(bt_frame, cfg, mono_font, small_font, header_font)

        self._balance    = 0.0
        self._free_slots = cfg["trading"]["max_open_slots"]
        self._max_slots  = cfg["trading"]["max_open_slots"]

        self._refresh()

    def _build_live_tab(self, parent, mono_font, header_font, small_font):
        self._mono_font   = mono_font
        self._small_font  = small_font
        self._header_font = header_font
        self._sort_col    = None
        self._sort_dir    = 1

        toolbar = tk.Frame(parent, bg=BG, pady=3)
        toolbar.pack(fill="x", padx=6)
        tk.Label(toolbar, text="Keresés:", bg=BG, fg=FG_GRAY,
                 font=small_font).pack(side="left")
        self._search_var = tk.StringVar()
        self._search_var.trace_add("write", lambda *_: self._apply_filter_sort())
        tk.Entry(toolbar, textvariable=self._search_var, width=12,
                 bg=BG_HEADER, fg=FG_WHITE, font=small_font,
                 insertbackground=FG_WHITE, relief="flat").pack(side="left", padx=(3, 12))
        self._hide_stopped_var = tk.BooleanVar(value=False)
        tk.Checkbutton(toolbar, text="STOPPED elrejtése", variable=self._hide_stopped_var,
                       bg=BG, fg=FG_GRAY, selectcolor=BG_HEADER,
                       activebackground=BG, activeforeground=FG_WHITE, font=small_font,
                       command=self._apply_filter_sort).pack(side="left", padx=4)
        tk.Button(toolbar, text="  +  Instrumentum", font=small_font,
                  bg=BTN_OPT_BG, fg=BTN_OPT_FG, relief="flat", cursor="hand2",
                  command=self._show_add_instrument).pack(side="right", padx=4)
        tk.Button(toolbar, text="  ⚙  Beállítás", font=small_font,
                  bg=BG_INACTIVE, fg=FG_WHITE, relief="flat", cursor="hand2",
                  command=self._show_settings).pack(side="right", padx=4)

        legend = tk.Frame(parent, bg=BG, pady=2)
        legend.pack(fill="x", padx=6)
        for text, col in [
            ("■ LIVE", FG_GREEN), ("■ STOPPED", FG_GRAY),
            ("■ Nem tanított", FG_GRAY_DIM),
            ("■ Optimalizálás", FG_YELLOW), ("✦ Kockázatmentes", FG_CYAN),
            ("R Risky", FG_ORANGE),
        ]:
            tk.Label(legend, text=text, bg=BG, fg=col, font=small_font, padx=6).pack(side="left")

        # ── Visszaszámláló-sáv (közös, minden instrumentumnál azonos) ───────
        # Config-vezérelt: dashboard.countdown_timeframes (percek listája) vagy
        # üres → a stratégia összes időkerete.
        strat_tfs = {tf.minutes: tf.label for tf in self.strategy.timeframes()}
        cd_cfg = self.cfg.get("dashboard", {}).get("countdown_timeframes")
        if cd_cfg:
            self._countdown_tfs = [(m, strat_tfs.get(m, f"{m}p")) for m in cd_cfg
                                   if m in strat_tfs]
        else:
            self._countdown_tfs = [(tf.minutes, tf.label) for tf in self.strategy.timeframes()]
        self._countdown_lbls = {}
        for minutes, label in self._countdown_tfs:
            lbl = tk.Label(legend, text=f"{label} zárás: --:--", bg=BG,
                           fg=FG_CYAN, font=header_font, padx=8)
            lbl.pack(side="right")
            self._countdown_lbls[minutes] = lbl

        tk.Frame(parent, bg=FG_GRAY_DIM, height=1).pack(fill="x", padx=2, pady=2)

        # ── Görgethető tábla: rögzített fejléc + scrollozható sorok ─────────
        table_holder = tk.Frame(parent, bg=BG)
        table_holder.pack(fill="both", expand=True, padx=2)

        header_holder = tk.Frame(table_holder, bg=BG)
        header_holder.pack(fill="x")
        self._header_row = HeaderRow(
            header_holder, self._columns, header_font, small_font,
            on_col_click=self._on_header_click)

        canvas = tk.Canvas(table_holder, bg=BG, highlightthickness=0)
        vsb = tk.Scrollbar(table_holder, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        self._table_frame = tk.Frame(canvas, bg=BG)   # ide kerülnek a sorok
        canvas.create_window((0, 0), window=self._table_frame, anchor="nw")
        self._table_frame.bind(
            "<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

        # Egérgörgő csak akkor görget, ha a kurzor a tábla fölött van
        def _on_wheel(e):
            canvas.yview_scroll(int(-e.delta / 120), "units")
        canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", _on_wheel))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))

        self.rows: dict[str, PairRow] = {}
        for idx, (symbol, pair_cfg) in enumerate(self.cfg["pairs"].items()):
            if not isinstance(pair_cfg, dict):
                continue
            self.rows[symbol] = PairRow(
                self._table_frame, symbol, idx, self._columns,
                on_run=self._handle_run, on_opt=self._handle_opt,
                on_delete=self._handle_delete, on_risky=self._handle_risky,
                on_name_click=self._show_instrument_params,
                mono_font=mono_font, small_font=small_font)

        self._apply_filter_sort()

        tk.Frame(parent, bg=FG_GRAY_DIM, height=1).pack(fill="x", padx=2, pady=2)
        self.lbl_status = tk.Label(parent, text="Indulás...", bg=BG, fg=FG_GRAY, font=small_font)
        self.lbl_status.pack(side="bottom", pady=4)

    def _on_header_click(self, col_idx: int):
        if self._sort_col == col_idx:
            if self._sort_dir == 1:
                self._sort_dir = -1
            else:
                self._sort_col = None
                self._sort_dir = 1
        else:
            self._sort_col = col_idx
            self._sort_dir = 1
        self._header_row.set_sort(self._sort_col, self._sort_dir)
        self._apply_filter_sort()

    @staticmethod
    def _sortable(v):
        """Vegyes típusú értékeket összehasonlíthatóvá tesz: (rang, érték)."""
        if isinstance(v, (int, float)):
            return (0, v)
        s = str(v)
        try:
            return (0, float(s.replace("%", "").replace("$", "")
                              .replace("+", "").replace("▲", "").replace("▼", "").strip()))
        except ValueError:
            return (1, s)

    def _sort_key(self, symbol: str):
        if self._sort_col is None:
            return (0, symbol)
        key = self._columns[self._sort_col].key
        ds  = self.dashboard_ref.get(symbol)
        if ds is None:
            return (1, "")
        if key == "symbol":
            return (0, symbol)
        if key == "bid":      return self._sortable(ds.bid if ds.bid is not None else 0)
        if key == "ask":      return self._sortable(ds.ask if ds.ask is not None else 0)
        if key == "change":   return self._sortable(ds.change_pct if ds.change_pct is not None else 0)
        if key == "spread":   return self._sortable(ds.spread_pts)
        if key == "position": return self._sortable(ds.position_pnl if ds.position_pnl is not None else 0)
        if key == "daily":    return self._sortable(ds.daily_pnl)
        if key == "opt":      return self._sortable(self.optimizer_status.get(symbol, ""))
        col = self._columns[self._sort_col]
        if col.kind == "countdown":
            return self._sortable(ds.timeframe_remaining.get(col.timeframe_min, 0))
        cell = ds.strategy_cells.get(key)
        return self._sortable(cell[0] if cell else "—")

    def _apply_filter_sort(self):
        search = self._search_var.get().upper().strip() if hasattr(self, "_search_var") else ""
        hide_stopped = self._hide_stopped_var.get() if hasattr(self, "_hide_stopped_var") else False

        visible = []
        for symbol in self.rows:
            if search and search not in symbol.upper():
                continue
            st = self.instrument_state.get(symbol, "STOPPED")
            if hide_stopped and st == "STOPPED":
                continue
            visible.append(symbol)

        if self._sort_col is not None:
            visible.sort(key=self._sort_key, reverse=(self._sort_dir == -1))

        for sym in self.rows:
            self.rows[sym].frame.pack_forget()
        for sym in visible:
            self.rows[sym].frame.pack(fill="x", padx=2, pady=0)

    # ── Instrumentum hozzáadása ──────────────────────────────────────────
    def _show_add_instrument(self):
        popup = tk.Toplevel(self.root)
        popup.title("Instrumentum hozzáadása")
        popup.configure(bg=BG)
        popup.resizable(False, False)
        popup.grab_set()
        tk.Label(popup, text="Elérhető szimbólumok (MT5):", bg=BG, fg=FG_BLUE,
                 font=self._header_font).pack(padx=12, pady=(10, 4), anchor="w")
        search_var = tk.StringVar()
        tk.Entry(popup, textvariable=search_var, width=28, bg=BG_HEADER, fg=FG_WHITE,
                 font=self._small_font, insertbackground=FG_WHITE,
                 relief="flat").pack(padx=12, pady=(0, 6))
        in_config = set(self.rows.keys())
        available: list = []   # háttérszálból töltődik

        frame_lb = tk.Frame(popup, bg=BG)
        frame_lb.pack(padx=12, fill="both", expand=True)
        scrollbar = tk.Scrollbar(frame_lb)
        scrollbar.pack(side="right", fill="y")
        listbox = tk.Listbox(frame_lb, width=30, height=18, bg=BG_HEADER, fg=FG_WHITE,
                             selectbackground=BTN_OPT_BG, font=self._small_font,
                             relief="flat", yscrollcommand=scrollbar.set)
        listbox.pack(side="left", fill="both", expand=True)
        scrollbar.config(command=listbox.yview)

        def refresh_list(*_):
            q = search_var.get().upper()
            listbox.delete(0, "end")
            for s in available:
                if q in s.upper():
                    listbox.insert("end", s)
        search_var.trace_add("write", refresh_list)

        lbl_info = tk.Label(popup, text="Szimbólumok betöltése...", bg=BG,
                            fg=FG_GRAY, font=self._small_font)
        lbl_info.pack(pady=(4, 0))

        # MT5 szimbólum-lekérés HÁTTÉRSZÁLON; a UI-t after(0)-val frissítjük.
        def _load_syms():
            try:
                import MetaTrader5 as mt5
                syms = mt5.symbols_get()
                all_syms = sorted(s.name for s in syms) if syms else []
            except Exception:
                all_syms = []
            result = [s for s in all_syms if s not in in_config]

            def _apply():
                if not popup.winfo_exists():
                    return
                available[:] = result
                refresh_list()
                if not result:
                    lbl_info.config(
                        text="Minden MT5 szimbólum már szerepel a listában.", fg=FG_YELLOW)
                else:
                    lbl_info.config(text=f"{len(result)} elérhető szimbólum.", fg=FG_GRAY)
            try:
                self.root.after(0, _apply)
            except Exception:
                pass
        threading.Thread(target=_load_syms, daemon=True, name="MT5Symbols").start()

        def add_selected():
            sel = listbox.curselection()
            if not sel:
                return
            self._add_instrument(listbox.get(sel[0]))
            popup.destroy()

        btn_frame = tk.Frame(popup, bg=BG)
        btn_frame.pack(pady=10)
        tk.Button(btn_frame, text="Hozzáadás", bg=BTN_PLAY_BG, fg=BTN_PLAY_FG,
                  font=self._small_font, relief="flat",
                  command=add_selected).pack(side="left", padx=6)
        tk.Button(btn_frame, text="Mégse", bg=BTN_DIS_BG, fg=BTN_DIS_FG,
                  font=self._small_font, relief="flat",
                  command=popup.destroy).pack(side="left", padx=6)
        listbox.bind("<Double-Button-1>", lambda _: add_selected())

    def _add_instrument(self, symbol: str):
        if symbol in self.rows:
            return

        # MT5 symbol_info lekérés HÁTTÉRSZÁLON (MT5_LOCK alatt), majd a config-írás
        # és a widget-építés a FŐ szálon (tkinter csak onnan biztonságos).
        def _work():
            pip_size, pv1_usd, spread_pips = 0.0001, 10.0, 1.5
            try:
                import MetaTrader5 as _mt5
                from core.mt5_connector import MT5_LOCK
                with MT5_LOCK:
                    info = _mt5.symbol_info(symbol)
                if info:
                    d = info.digits
                    if d in (4, 5):
                        pip_size = info.point * 10
                    elif d in (2, 3):
                        pip_size = info.point * 100
                    else:
                        pip_size = info.point
                    tv, ts = info.trade_tick_value, info.trade_tick_size
                    pv1_usd = round(tv / ts * pip_size, 4) if ts > 0 else tv
                    spread_pips = round(info.spread * info.point / pip_size, 1) \
                                  if pip_size > 0 else 1.5
            except Exception:
                pass
            try:
                self.root.after(
                    0, lambda: self._finalize_add_instrument(
                        symbol, pip_size, pv1_usd, spread_pips))
            except Exception:
                pass
        threading.Thread(target=_work, daemon=True, name="MT5AddInstr").start()

    def _finalize_add_instrument(self, symbol, pip_size, pv1_usd, spread_pips):
        """A fő szálon fut: config-írás + dashboard state + új tábla-sor."""
        if symbol in self.rows:
            return
        self.cfg["pairs"][symbol] = {
            "enabled": False, "pip_size": pip_size, "pv1_usd": pv1_usd,
            "backtest_spread_pips": spread_pips, "sess_start": 0, "sess_end": 24,
        }
        try:
            with open(ROOT / "config.json", "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

        from trading.live_trader import PairDashboardState
        self.dashboard_ref[symbol] = PairDashboardState(
            symbol=symbol, trained=False, enabled=False)
        self.instrument_state[symbol] = "STOPPED"
        self.optimizer_status[symbol] = ""

        idx = len(self.rows)
        self.rows[symbol] = PairRow(
            self._table_frame, symbol, idx, self._columns,
            on_run=self._handle_run, on_opt=self._handle_opt,
            on_delete=self._handle_delete, on_risky=self._handle_risky,
            on_name_click=self._show_instrument_params,
            mono_font=self._mono_font, small_font=self._small_font)
        self._apply_filter_sort()

    # ── JSON szintaxis-színezés (Text widgethez) ─────────────────────────
    @staticmethod
    def _highlight_json(text):
        import re
        content = text.get("1.0", "end-1c")
        for tag in ("json_key", "json_str", "json_num", "json_bool"):
            text.tag_remove(tag, "1.0", "end")
        token = re.compile(
            r'"(?:\\.|[^"\\])*"'                  # idézőjeles szöveg
            r'|-?\d+\.?\d*(?:[eE][+-]?\d+)?'      # szám
            r'|\b(?:true|false|null)\b')          # logikai / null
        for m in token.finditer(content):
            s, e, tok = m.start(), m.end(), m.group()
            if tok[0] == '"':
                after = content[e:e + 8].lstrip()
                tag = "json_key" if after.startswith(":") else "json_str"
            elif tok in ("true", "false", "null"):
                tag = "json_bool"
            else:
                tag = "json_num"
            text.tag_add(tag, f"1.0+{s}c", f"1.0+{e}c")

    # ── Beállítás-szerkesztő (config.json) ───────────────────────────────
    def _show_settings(self):
        popup = tk.Toplevel(self.root)
        popup.title("Beállítások — config.json")
        popup.configure(bg=BG)
        popup.geometry("720x640")
        popup.grab_set()
        tk.Label(popup, text="config.json szerkesztése (mentéskor JSON-validálás):",
                 bg=BG, fg=FG_BLUE, font=self._header_font).pack(anchor="w", padx=10, pady=(10, 2))
        tk.Label(popup, text="Megjegyzés: a kereskedési paraméterek menet közben "
                 "érvényesülnek; a párok listája / stratégia újraindítást igényel.",
                 bg=BG, fg=FG_GRAY, font=self._small_font).pack(anchor="w", padx=10)

        txt_frame = tk.Frame(popup, bg=BG)
        txt_frame.pack(fill="both", expand=True, padx=10, pady=4)
        sb = tk.Scrollbar(txt_frame)
        sb.pack(side="right", fill="y")
        text = tk.Text(txt_frame, bg=BG_HEADER, fg=FG_WHITE, insertbackground=FG_WHITE,
                       font=self._mono_font, wrap="none", yscrollcommand=sb.set)
        text.pack(side="left", fill="both", expand=True)
        sb.config(command=text.yview)
        # JSON szintaxis-színezés
        text.tag_configure("json_key",  foreground=FG_BLUE)
        text.tag_configure("json_str",  foreground=FG_GREEN)
        text.tag_configure("json_num",  foreground=FG_ORANGE)
        text.tag_configure("json_bool", foreground=FG_CYAN)
        text.insert("1.0", json.dumps(self.cfg, indent=2, ensure_ascii=False))
        self._highlight_json(text)

        # Élő újraszínezés szerkesztés közben (debounce-olva, hogy ne akadjon)
        def _schedule_hl(_event=None):
            prev = getattr(self, "_hl_after_id", None)
            if prev:
                try:
                    popup.after_cancel(prev)
                except Exception:
                    pass
            self._hl_after_id = popup.after(200, lambda: self._highlight_json(text))
        text.bind("<KeyRelease>", _schedule_hl)

        lbl_err = tk.Label(popup, text="", bg=BG, fg=FG_RED, font=self._small_font)
        lbl_err.pack(anchor="w", padx=10)

        def save():
            try:
                new = json.loads(text.get("1.0", "end"))
            except Exception as e:
                lbl_err.config(text=f"Érvénytelen JSON: {e}")
                return
            try:
                with open(ROOT / "config.json", "w", encoding="utf-8") as f:
                    json.dump(new, f, indent=2, ensure_ascii=False)
            except Exception as e:
                lbl_err.config(text=f"Mentési hiba: {e}")
                return
            # In-place frissítés → a live_trader ugyanazt a dict-et látja
            self.cfg.clear()
            self.cfg.update(new)
            popup.destroy()

        btns = tk.Frame(popup, bg=BG)
        btns.pack(pady=10)
        tk.Button(btns, text="Mentés", bg=BTN_PLAY_BG, fg=BTN_PLAY_FG, relief="flat",
                  font=self._small_font, command=save).pack(side="left", padx=6)
        tk.Button(btns, text="Mégse", bg=BTN_DIS_BG, fg=BTN_DIS_FG, relief="flat",
                  font=self._small_font, command=popup.destroy).pack(side="left", padx=6)

    # ── Optimalizált paraméterek szerkesztője (instrumentum nevére kattintva) ─
    def _show_instrument_params(self, symbol: str):
        from ml.optimizer import PARAMS_DIR
        pf = PARAMS_DIR / f"{symbol}.json"
        if not pf.exists():
            return   # csak optimalizált párra
        try:
            with open(pf, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return
        params = data.get("params", {})

        popup = tk.Toplevel(self.root)
        popup.title(f"{symbol} — optimalizált paraméterek")
        popup.configure(bg=BG)
        popup.grab_set()

        ts = data.get("test_summary", {})
        if ts:
            tk.Label(popup,
                     text=f"Teszt: {ts.get('trades',0)} trade   "
                          f"P&L {ts.get('total_pnl',0):+.0f}$   "
                          f"Win {ts.get('win_rate',0)*100:.0f}%   "
                          f"MaxDD {ts.get('max_drawdown',0)*100:.1f}%",
                     bg=BG, fg=FG_CYAN, font=self._small_font).pack(anchor="w", padx=10, pady=(10, 2))
        tk.Label(popup, text="Kézi módosítás — a következő Play-nél lép életbe:",
                 bg=BG, fg=FG_GRAY, font=self._small_font).pack(anchor="w", padx=10)

        form = tk.Frame(popup, bg=BG)
        form.pack(fill="both", expand=True, padx=10, pady=6)
        entries = {}
        keys = sorted(k for k in params if not k.startswith("_"))
        for i, k in enumerate(keys):
            tk.Label(form, text=k, bg=BG, fg=FG_WHITE, font=self._small_font,
                     anchor="w", width=24).grid(row=i, column=0, sticky="w", pady=1)
            e = tk.Entry(form, width=14, bg=BG_HEADER, fg=FG_WHITE,
                         font=self._small_font, insertbackground=FG_WHITE)
            e.insert(0, str(params[k]))
            e.grid(row=i, column=1, padx=6, pady=1)
            entries[k] = e

        lbl_err = tk.Label(popup, text="", bg=BG, fg=FG_RED, font=self._small_font)
        lbl_err.pack(anchor="w", padx=10)

        def save():
            new_params = dict(params)
            for k, e in entries.items():
                v = e.get().strip()
                orig = params[k]
                try:
                    if isinstance(orig, bool):
                        new_params[k] = v.lower() in ("true", "1", "igen", "yes")
                    elif isinstance(orig, int):
                        new_params[k] = int(float(v))
                    elif isinstance(orig, float):
                        new_params[k] = float(v)
                    else:
                        new_params[k] = v
                except ValueError:
                    lbl_err.config(text=f"Hibás érték: {k} = {v!r}")
                    return
            data["params"] = new_params
            data["manually_edited_at"] = datetime.utcnow().isoformat()
            try:
                tmp = pf.with_suffix(".tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False, default=str)
                tmp.replace(pf)
            except Exception as ex:
                lbl_err.config(text=f"Mentési hiba: {ex}")
                return
            popup.destroy()

        btns = tk.Frame(popup, bg=BG)
        btns.pack(pady=10)
        tk.Button(btns, text="Mentés", bg=BTN_PLAY_BG, fg=BTN_PLAY_FG, relief="flat",
                  font=self._small_font, command=save).pack(side="left", padx=6)
        tk.Button(btns, text="Mégse", bg=BTN_DIS_BG, fg=BTN_DIS_FG, relief="flat",
                  font=self._small_font, command=popup.destroy).pack(side="left", padx=6)

    # ── Gomb handlerek ────────────────────────────────────────────────────
    def _handle_run(self, symbol: str):
        """A futtató gomb (Play↔Stop morph) kezelője."""
        st = self.instrument_state.get(symbol)
        if st == "STOPPED":
            self._handle_play(symbol)
        elif st == "LIVE":
            self._handle_stop(symbol)

    def _handle_play(self, symbol: str):
        ds = self.dashboard_ref.get(symbol)
        if ds is None or not ds.trained:
            return
        if self.instrument_state.get(symbol) != "STOPPED":
            return
        self.instrument_state[symbol] = "LIVE"
        if self._on_play:
            self._on_play(symbol)

    def _handle_stop(self, symbol: str):
        ds = self.dashboard_ref.get(symbol)
        if ds is None:
            return
        if self.instrument_state.get(symbol) != "LIVE":
            return
        if ds.position_pnl is not None:
            return
        self.instrument_state[symbol] = "STOPPED"
        if self._on_stop:
            self._on_stop(symbol)

    def _handle_opt(self, symbol: str):
        """OPT↔STOP morph: STOPPED → optimalizálás indítása; QUEUED → sorból törlés."""
        st = self.instrument_state.get(symbol)
        if st == "STOPPED":
            self._opt_ctrl.request_optimize(symbol)
        elif st == "QUEUED":
            self._opt_ctrl.cancel_queued(symbol)
        else:
            return
        row = self.rows.get(symbol)
        ds  = self.dashboard_ref.get(symbol)
        if row and ds:
            row.update(ds, self.instrument_state.get(symbol, "STOPPED"),
                       self.optimizer_status.get(symbol, ""),
                       connected=getattr(self, "_connected", True))

    def _handle_risky(self, symbol: str):
        """Risky mód váltása — azonnal menti a data/risky_mode.json-ba."""
        from core import risky_mode
        new_val = risky_mode.toggle(symbol)
        ds = self.dashboard_ref.get(symbol)
        if ds is not None:
            ds.risky = new_val
            row = self.rows.get(symbol)
            if row is not None:
                row.update(ds, self.instrument_state.get(symbol, "STOPPED"),
                           self.optimizer_status.get(symbol, ""),
                           connected=getattr(self, "_connected", False))

    def _handle_delete(self, symbol: str):
        """Instrumentum törlése a config-ból és a táblából (megerősítéssel).
        Csak megállított (STOPPED) párra engedélyezett."""
        if self.instrument_state.get(symbol) != "STOPPED":
            return
        from tkinter import messagebox
        if not messagebox.askyesno(
                "Törlés megerősítése",
                f"Biztosan törlöd a(z) {symbol} instrumentumot a listából?"):
            return
        self.cfg["pairs"].pop(symbol, None)
        try:
            with open(ROOT / "config.json", "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, indent=2, ensure_ascii=False)
        except Exception:
            pass
        row = self.rows.pop(symbol, None)
        if row is not None:
            row.frame.destroy()
        self.dashboard_ref.pop(symbol, None)
        self.instrument_state.pop(symbol, None)
        self.optimizer_status.pop(symbol, None)
        self._apply_filter_sort()

    # ── Pozíciókezelő handlerek (Pozíciók fül) ──────────────────────────
    def _pos_panic(self, ticket: int):
        from tkinter import messagebox
        if not messagebox.askyesno("Pozíció zárása",
                                   f"Biztosan lezárod a #{ticket} pozíciót?"):
            return
        def _w():
            from core import mt5_connector
            mt5_connector.close_position(ticket)
        threading.Thread(target=_w, daemon=True, name="PanicClose").start()

    def _pos_close_all(self):
        from tkinter import messagebox
        positions = getattr(self, "_mt5_cache", {}).get("positions_detail", [])
        if not positions:
            return
        if not messagebox.askyesno(
                "ÖSSZES pozíció zárása",
                f"Biztosan lezárod MIND a {len(positions)} nyitott pozíciót?"):
            return
        tickets = [p["ticket"] for p in positions]
        def _w():
            from core import mt5_connector
            for t in tickets:
                mt5_connector.close_position(t)
        threading.Thread(target=_w, daemon=True, name="CloseAll").start()

    def _pos_be(self, ticket: int):
        pos = next((p for p in getattr(self, "_mt5_cache", {}).get("positions_detail", [])
                    if p["ticket"] == ticket), None)
        if not pos:
            return
        orig_sl = pos["sl"]
        def _w():
            from core import mt5_connector
            from trading.live_trader import position_state
            # BE + spread puffer (spread×2 → ×1 → pontos BE), nem pontos entry
            if mt5_connector.move_to_breakeven(ticket):
                st = position_state.setdefault(
                    ticket, {"original_sl": orig_sl, "trailing_enabled": True, "be_done": False})
                st["be_done"] = True
        threading.Thread(target=_w, daemon=True, name="ManualBE").start()

    def _pos_trail(self, ticket: int):
        from trading.live_trader import position_state
        st = position_state.setdefault(
            ticket, {"original_sl": 0.0, "trailing_enabled": True, "be_done": False})
        st["trailing_enabled"] = not st.get("trailing_enabled", True)

    def _handle_connect(self):
        # A connect() blokkoló MT5-login — háttérszálon, hogy a UI ne fagyjon.
        # Az eredményt a bg-poller (5 mp) és a _refresh úgyis felkapja a cache-ből.
        self.lbl_conn.config(text="● Kapcsolódás...", fg=FG_YELLOW)

        def _work():
            try:
                from core import mt5_connector
                mt5_connector.connect(self.cfg)
            except Exception:
                pass
        threading.Thread(target=_work, daemon=True, name="MT5Connect").start()

    # ── Publikus API ──────────────────────────────────────────────────────
    def set_balance(self, balance: float):
        self._balance = balance

    def set_slots(self, free: int, max_s: int):
        self._free_slots = free
        self._max_slots  = max_s

    # ── Kapcsolat UI ────────────────────────────────────────────────────
    def _update_connection_ui(self, info: dict):
        self._connected = info.get("connected", False)
        if info["connected"]:
            demo_tag = "  [DEMO]" if info.get("is_demo") else "  [ÉLES!]"
            demo_fg  = FG_YELLOW if info.get("is_demo") else FG_RED
            self.lbl_conn.config(text="● Online", fg=FG_GREEN)
            self.lbl_account.config(
                text=f"#{info['login']}  {info['server']}{demo_tag}", fg=demo_fg)
            self._btn_connect.pack_forget()
            if info["balance"] > 0:
                self._balance = info["balance"]
                cur = info.get("currency", "")
                self.lbl_balance.config(text=f"Egyenleg: {info['balance']:,.2f} {cur}")
        else:
            self.lbl_conn.config(text="● Offline", fg=FG_RED)
            broker = self.cfg.get("broker", {})
            demo_tag = "  [DEMO]" if broker.get("is_demo") else "  [ÉLES]"
            self.lbl_account.config(
                text=f"#{broker.get('login','—')}  {broker.get('server','—')}{demo_tag}",
                fg=FG_GRAY)
            self._btn_connect.pack(side="right", padx=6)

    # ── Piaci adat háttérszál (egységes) ────────────────────────────────
    def _start_market_data_poll(self):
        if hasattr(self, "_poll_running"):
            return
        self._poll_running = True
        threading.Thread(target=self._market_data_loop, daemon=True,
                         name="MarketData").start()

    def _market_data_loop(self):
        import time as _time
        _time.sleep(5)  # UI stabilizálódjon
        price_sec  = max(1, self._price_refresh_sec)
        live_every = max(1, round(self._fast_refresh_sec / price_sec))
        all_every  = max(1, round(self._all_refresh_sec  / price_sec))
        counter = 0
        while getattr(self, "_poll_running", False):
            # MT5 nem thread-safe — ne fusson míg optimizer dolgozik
            if not self._opt_ctrl._running:
                all_syms = [s for s in self.dashboard_ref
                            if isinstance(self.cfg["pairs"].get(s), dict)]
                # 1) Olcsó ár-frissítés MINDEN párra (gyakran) — ez tartja
                #    naprakészen a BID/ASK/Vált.%/Spread-et minden instrumentumon.
                for sym in all_syms:
                    if self._opt_ctrl._running:
                        break
                    try:
                        self._refresh_price(sym)
                    except Exception:
                        pass
                # 2) Drága indikátor-számítás: minden pár ritkán, LIVE gyakrabban.
                if counter % all_every == 0:
                    ind_targets = all_syms
                elif counter % live_every == 0:
                    ind_targets = [s for s, st in self.instrument_state.items()
                                   if st == "LIVE"]
                else:
                    ind_targets = []
                for sym in ind_targets:
                    if self._opt_ctrl._running:
                        break
                    try:
                        self._refresh_pair_data(sym)
                    except Exception:
                        pass
            counter += 1
            _time.sleep(price_sec)

    def _refresh_price(self, symbol: str):
        """Olcsó ár-frissítés: BID/ASK/tizedes/spread + napi változás%.
        Bars/indikátor NÉLKÜL → minden párra futtatható gyakran. A symbol_select
        biztosítja, hogy a (akár letiltott) szimbólum is streameljen MT5-ben."""
        try:
            import MetaTrader5 as _mt5
            from core.mt5_connector import MT5_LOCK
        except Exception:
            return
        ds = self.dashboard_ref.get(symbol)
        if ds is None:
            return
        with MT5_LOCK:
            _mt5.symbol_select(symbol, True)
            tick = _mt5.symbol_info_tick(symbol)
            info = _mt5.symbol_info(symbol)
        if tick and tick.bid:
            ds.prev_bid, ds.prev_ask = ds.bid, ds.ask
            ds.bid, ds.ask = tick.bid, tick.ask
        if info:
            ds.digits     = info.digits
            ds.spread_pts = info.spread
        ref = ds.bid if ds.bid is not None else ds.ask
        if ref is not None and ds.day_open:
            ds.change_pct = (ref - ds.day_open) / ds.day_open * 100.0

    # MT5 timeframe leképezés perc → konstans (lazán, futásidőben)
    @staticmethod
    def _mt5_timeframe(mt5, minutes: int):
        table = {
            1:   mt5.TIMEFRAME_M1,   5:   mt5.TIMEFRAME_M5,
            15:  mt5.TIMEFRAME_M15,  30:  mt5.TIMEFRAME_M30,
            60:  mt5.TIMEFRAME_H1,   240: mt5.TIMEFRAME_H4,
        }
        return table.get(minutes, mt5.TIMEFRAME_M1)

    def _refresh_pair_data(self, symbol: str):
        """Egy pár piaci adatainak frissítése MT5-ből: ár, spread, változás%,
        és a stratégia megjelenítési cellái."""
        try:
            import MetaTrader5 as _mt5
            from core.mt5_connector import MT5_LOCK
        except Exception:
            return
        import pandas as _pd
        from strategy.base import MarketData
        from ml.optimizer import PARAMS_DIR

        ds = self.dashboard_ref.get(symbol)
        if ds is None or not isinstance(self.cfg["pairs"].get(symbol), dict):
            return

        # Paraméterek: optimalizált, ha van; egyébként alap.
        params_f = PARAMS_DIR / f"{symbol}.json"
        if params_f.exists():
            with open(params_f, encoding="utf-8") as f:
                params = json.load(f).get("params", {})
            ds.trained = True
            # Külsőleg (más app által) optimalizált párt is "vegyük észre":
            # ha nem épp most optimalizál, jelezzük késznek.
            if self.instrument_state.get(symbol) not in ("OPTIMIZING", "QUEUED"):
                self.optimizer_status[symbol] = "Kész ✓"
        else:
            params = self.strategy.base_params(self.cfg)
            ds.trained = False
            if not params.get("sma_period"):
                return

        timeframes = self.strategy.timeframes()

        primary = timeframes[0].label  # a "fő" időkeret (ATR-hez)

        with MT5_LOCK:
            _mt5.symbol_select(symbol, True)   # streameljen akkor is, ha letiltott
            raw_bars = {}
            for tf in timeframes:
                warmup = self.strategy.warmup_bars(params, tf.label)
                raw_bars[tf.label] = _mt5.copy_rates_from_pos(
                    symbol, self._mt5_timeframe(_mt5, tf.minutes), 0, warmup)
            info = _mt5.symbol_info(symbol)
            d1   = _mt5.copy_rates_from_pos(symbol, _mt5.TIMEFRAME_D1, 0, 1)

        # Ha nincs gyertyaadat (pl. demo / offline) → ne írjuk felül a cellákat.
        # (Az ár ettől függetlenül friss marad a _refresh_price révén.)
        if any(raw_bars[tf.label] is None for tf in timeframes):
            return

        bars = {}
        for label, arr in raw_bars.items():
            df = _pd.DataFrame(arr)
            df["time"] = _pd.to_datetime(df["time"], unit="s", utc=True)
            df.set_index("time", inplace=True)
            bars[label] = df

        md = MarketData(symbol=symbol, params=params, bars=bars)
        try:
            cells = self.strategy.compute_display(md)
            ds.strategy_cells = {k: (c.text, c.color) for k, c in cells.items()}
        except Exception:
            pass

        # Max spread (ATR-alapú) — a fő időkeret ATR-jéből
        if info and info.point > 0:
            try:
                from core.indicator_engine import atr as _atr
                dfp = bars.get(primary)
                if dfp is not None and len(dfp) > 2:
                    atr_ser = _atr(dfp["high"], dfp["low"], dfp["close"],
                                   params.get("atr_period", 14))
                    atr_val = atr_ser.iloc[-2]
                    if atr_val == atr_val:   # not NaN
                        atr_pts = int(atr_val / info.point)
                        ratio   = params.get("max_spread_atr_ratio", 0.20)
                        ds.max_spread_pts = max(1, int(atr_pts * ratio))
            except Exception:
                pass

        # Napi nyitóár (változás% alaphoz) — a _refresh_price ebből számol
        if d1 is not None and len(d1) > 0:
            ds.day_open = float(d1[-1]["open"])

    # ── Account háttérszál ────────────────────────────────────────────────
    def _start_bg_poller(self):
        if hasattr(self, "_bg_poller_running"):
            return
        self._bg_poller_running = True
        self._mt5_cache = {"connected": False, "info": {}, "daily_pnl": None,
                           "positions": {}, "positions_detail": []}

        def _loop():
            import time as _t
            while getattr(self, "_bg_poller_running", False):
                try:
                    from core.mt5_connector import (
                        connection_info, daily_pnl as _dpnl,
                        open_positions_by_symbol, open_positions_detailed)
                    info = connection_info(self.cfg)
                    self._mt5_cache["connected"] = info.get("connected", False)
                    self._mt5_cache["info"]      = info
                    if info.get("connected"):
                        self._mt5_cache["daily_pnl"] = _dpnl()
                        self._mt5_cache["positions"] = open_positions_by_symbol()
                        self._mt5_cache["positions_detail"] = open_positions_detailed()
                    else:
                        self._mt5_cache["daily_pnl"] = None
                        self._mt5_cache["positions"] = {}
                        self._mt5_cache["positions_detail"] = []
                except Exception:
                    pass
                _t.sleep(5)
        threading.Thread(target=_loop, daemon=True, name="MT5BgPoller").start()

    # ── Fő frissítés (1 mp, csak Python — nem blokkol MT5-re) ────────────
    def _refresh(self):
        now = datetime.now(timezone.utc)
        self.lbl_time.config(text=now.strftime("%Y-%m-%d %H:%M:%S UTC"))

        # Visszaszámlálók a stratégia időkereteire (közös felső sáv + per-pár állapot)
        try:
            from trading.live_trader import seconds_to_candle_close
            for tf in self.strategy.timeframes():
                rem = seconds_to_candle_close(tf.minutes)
                for ds in self.dashboard_ref.values():
                    ds.timeframe_remaining[tf.minutes] = rem
                lbl = getattr(self, "_countdown_lbls", {}).get(tf.minutes)
                if lbl is not None:
                    lbl.config(text=f"{tf.label} zárás: {rem//60}:{rem%60:02d}")
        except Exception:
            pass

        if not hasattr(self, "_conn_tick"):
            self._conn_tick = 0
            self._last_heartbeat = time.monotonic()
            risky_mode.load()                 # induló risky állapot
            self._start_bg_poller()
            self._start_market_data_poll()
            self._start_watchdog()
        self._conn_tick += 1

        # Risky állapot periodikus újraolvasása (külső program írhatja)
        if self._conn_tick % 60 == 0:
            risky_mode.load()

        cache     = getattr(self, "_mt5_cache", {})
        connected = cache.get("connected", False)
        mt5_info  = cache.get("info", {})
        mt5_pnl   = cache.get("daily_pnl", None)
        mt5_positions = cache.get("positions", {})

        if mt5_info:
            self._update_connection_ui(mt5_info)

        if self._balance > 0:
            cur = mt5_info.get("currency", "")
            self.lbl_balance.config(text=f"Egyenleg: {self._balance:,.2f} {cur}".rstrip())

        if mt5_pnl is not None:
            daily_total = mt5_pnl
            pnl_src = ""
        else:
            daily_total = sum(ds.daily_pnl for ds in self.dashboard_ref.values())
            pnl_src = " (demo)"
        self.lbl_daily.config(text=f"Napi P&L: {daily_total:+.2f}${pnl_src}",
                              fg=FG_GREEN if daily_total >= 0 else FG_RED)

        free = max(0, self._free_slots)
        self.lbl_slots.config(text=f"Szabad slotok: {free}/{self._max_slots}",
                              fg=FG_GREEN if free > 0 else FG_RED)

        total_daily = sum(ds.daily_pnl for ds in self.dashboard_ref.values())
        limit_hit = (self._balance > 0 and
                     total_daily <= -(self._balance * self.cfg["trading"]["daily_loss_limit_pct"]))
        self.lbl_limit.config(text="Napi limit: STOP" if limit_hit else "Napi limit: OK",
                              fg=FG_RED if limit_hit else FG_GREEN)

        if mt5_positions is not None:
            occupied = sum(p.get("count", 1) for p in mt5_positions.values())
            self._free_slots = max(0, self._max_slots - occupied)
            free = self._free_slots
            self.lbl_slots.config(text=f"Szabad slotok: {free}/{self._max_slots}",
                                  fg=FG_GREEN if free > 0 else FG_RED)

        live_count = 0
        if hasattr(self, "rows"):
            for symbol, row in self.rows.items():
                ds         = self.dashboard_ref.get(symbol)
                inst_state = self.instrument_state.get(symbol, "STOPPED")
                opt_status = self.optimizer_status.get(symbol, "")
                if ds is not None and mt5_positions is not None:
                    pos = mt5_positions.get(symbol)
                    ds.position_pnl = pos["pnl"] if pos else None
                    ds.pos_count    = pos.get("count", 1) if pos else 0
                    ds.risk_free    = pos["risk_free"] if pos else False
                if ds is not None:
                    ds.risky = risky_mode.is_risky(symbol)
                    row.update(ds, inst_state, opt_status,
                               connected=getattr(self, "_connected", False))
                if inst_state == "LIVE":
                    live_count += 1

        if hasattr(self, "lbl_status"):
            self.lbl_status.config(
                text=f"Utolsó frissítés: {now.strftime('%H:%M:%S')}  |  LIVE: {live_count}")

        # Pozíciók fül frissítése
        if hasattr(self, "_pos_tab"):
            try:
                self._pos_tab.refresh()
            except Exception:
                pass

        # Heartbeat: a teljes tick lefutott → a fő szál él
        self._last_heartbeat = time.monotonic()
        self.root.after(1000, self._refresh)

    # ── Fagyás-watchdog ──────────────────────────────────────────────────
    def _start_watchdog(self):
        """Háttérszál: jelzi, ha a fő (UI) szál túl sokáig nem lélegzett.
        A küszöb fölötti késés = a fő szálon blokkoló hívás (fejlesztői jelzés)."""
        if hasattr(self, "_watchdog_running"):
            return
        self._watchdog_running = True
        threshold = self.cfg.get("dashboard", {}).get("watchdog_threshold_sec", 2.0)

        def _loop():
            import logging as _logging
            log = _logging.getLogger("ui.watchdog")
            warned = False
            while getattr(self, "_watchdog_running", False):
                lag = time.monotonic() - getattr(self, "_last_heartbeat", time.monotonic())
                if lag > threshold:
                    if not warned:
                        msg = f"⚠ A FŐ SZÁL {lag:.1f} mp-ig nem frissült (blokkoló hívás?)."
                        log.warning(msg)
                        try:
                            with open(ROOT / "data" / "ui_watchdog.log", "a",
                                      encoding="utf-8") as f:
                                f.write(f"{datetime.now()}  {msg}\n")
                        except Exception:
                            pass
                        warned = True
                else:
                    warned = False
                time.sleep(0.5)
        threading.Thread(target=_loop, daemon=True, name="UIWatchdog").start()

    def run(self):
        try:
            self.root.mainloop()
        finally:
            try:
                self._opt_ctrl.shutdown()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Demo mód
# ---------------------------------------------------------------------------

def _demo_dashboard(cfg: dict):
    """Demo: UI layout + state machine bemutatása MT5 nélkül.
    A stratégia-cellákat szimulált értékekkel tölti, hogy az oszlopok lássanak."""
    import random
    from trading.live_trader import PairDashboardState

    strategy   = get_strategy(cfg)
    params_dir = ROOT / "data" / "optimized_params"
    real_trained = {f.stem for f in params_dir.glob("*.json")} if params_dir.exists() else set()
    symbols = [s for s, p in cfg["pairs"].items() if isinstance(p, dict)]

    states_pool = ["LIVE"] * 4 + ["STOPPED"] * 6
    random.shuffle(states_pool)

    db, inst_state, opt_status = {}, {}, {}
    strat_keys = [c.key for c in strategy.columns() if c.kind == "strategy"]

    for i, symbol in enumerate(symbols):
        trained = symbol in real_trained
        st      = states_pool[i % len(states_pool)] if trained else "STOPPED"
        inst_state[symbol] = st
        opt_status[symbol] = "Kész ✓" if trained else ""

        base = round(random.uniform(0.9, 1.6), 5)
        ds = PairDashboardState(
            symbol=symbol, enabled=trained, trained=trained,
            bid=base, ask=round(base + 0.0002, 5), prev_bid=base, prev_ask=base,
            digits=5, day_open=round(base * random.uniform(0.99, 1.01), 5),
            change_pct=round(random.uniform(-0.6, 0.6), 2),
            spread_pts=random.randint(6, 18), max_spread_pts=random.randint(12, 25),
            position_pnl=None, risk_free=False, daily_pnl=0.0,
        )
        # Stratégia-cellák szimulálása (csak LIVE pároknál mutatunk értéket)
        if st == "LIVE":
            for k in strat_keys:
                if "wpr" in k:
                    ds.strategy_cells[k] = (f"{random.uniform(-95,-5):.1f}", "white")
                elif "sig" in k:
                    sig = random.choice([("BUY▲", "green"), ("SELL▼", "red"), ("—", "muted")])
                    ds.strategy_cells[k] = sig
                else:  # sma_dir
                    d = random.choice([("BUY", "green"), ("SELL", "red"), ("—", "muted")])
                    ds.strategy_cells[k] = d
        for tf in strategy.timeframes():
            ds.timeframe_remaining[tf.minutes] = random.randint(0, tf.minutes * 60 - 1)
        db[symbol] = ds

    return db, inst_state, opt_status, 0


if __name__ == "__main__":
    cfg_path = ROOT / "config.json"
    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)
    db, inst_state, opt_status, n_pos = _demo_dashboard(cfg)
    win = DashboardWindow(cfg, db, inst_state, opt_status,
                          on_play_pair=None, on_stop_pair=None)
    max_s = cfg["trading"]["max_open_slots"]
    win.set_balance(1024.50)
    win.set_slots(free=max(0, max_s - n_pos), max_s=max_s)
    win.run()
