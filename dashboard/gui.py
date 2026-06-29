"""
Élő Dashboard — tkinter GUI

Fülek:
  [Live Dashboard]      — élő kereskedés táblázat, Play/Stop/OPT gombok
  [Portfólió Backtest]  — eszközválasztás, dátum, equity görbe, eredménytáblázat
"""

import json
import threading
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Színek
# ---------------------------------------------------------------------------

BG           = "#1e1e2e"
BG_HEADER    = "#181825"
BG_ROW_ODD   = "#1e1e2e"
BG_ROW_EVEN  = "#242438"
BG_INACTIVE  = "#2a2a3e"
BG_UNTRAINED = "#222230"
BG_OPT_ROW   = "#2a2a1e"
BG_BT        = "#1a1a2e"    # portfolio backtest háttér

FG_WHITE     = "#cdd6f4"
FG_GREEN     = "#a6e3a1"
FG_RED       = "#f38ba8"
FG_YELLOW    = "#f9e2af"
FG_GRAY      = "#585b70"
FG_GRAY_DIM  = "#45475a"
FG_BLUE      = "#89b4fa"
FG_CYAN      = "#89dceb"
FG_ORANGE    = "#fab387"

BTN_PLAY_BG  = "#40a02b"
BTN_PLAY_FG  = "#ffffff"
BTN_STOP_BG  = "#d20f39"
BTN_STOP_FG  = "#ffffff"
BTN_OPT_BG   = "#7287fd"
BTN_OPT_FG   = "#ffffff"
BTN_BT_BG    = "#e64553"    # portfolio BT gomb
BTN_BT_FG    = "#ffffff"
BTN_DIS_BG   = "#313244"
BTN_DIS_FG   = "#585b70"

CANVAS_BG    = "#11111b"
CANVAS_LINE  = "#a6e3a1"
CANVAS_REF   = "#585b70"


# ---------------------------------------------------------------------------
# Live Dashboard — oszlop definíciók
# ---------------------------------------------------------------------------

COLUMNS = [
    ("Instrumentum",  11, "w"),
    ("SMA irány",      8, "center"),
    ("M15 WPR",        7, "center"),
    ("M15 jelzés",     9, "center"),
    ("M15 hátr.",      7, "center"),
    ("M1 WPR",         7, "center"),
    ("M1 jelzés",      8, "center"),
    ("M1 hátr.",       7, "center"),
    ("Spread",         9, "center"),
    ("Pozíció",       10, "center"),
    ("Napi P&L",       9, "center"),
    ("Opt státusz",   12, "center"),
]


# ---------------------------------------------------------------------------
# Live Dashboard — egy sor widgetei
# ---------------------------------------------------------------------------

class PairRow:
    def __init__(self, parent: tk.Frame, symbol: str, row_idx: int,
                 on_play, on_stop, on_opt, mono_font, small_font):
        self.symbol   = symbol
        self._bg      = BG_ROW_ODD if row_idx % 2 == 0 else BG_ROW_EVEN

        self.frame = tk.Frame(parent, bg=self._bg)
        # Nem csomagoljuk magát — _apply_filter_sort() kezeli

        self.labels: list[tk.Label] = []
        for col_name, width, anchor in COLUMNS:
            lbl = tk.Label(self.frame, text="—", width=width, anchor=anchor,
                           bg=self._bg, fg=FG_GRAY, font=mono_font, padx=4, pady=3)
            lbl.pack(side="left")
            self.labels.append(lbl)

        self.btn_play = tk.Button(self.frame, text="▶", width=3,
                                  bg=BTN_DIS_BG, fg=BTN_DIS_FG, font=small_font,
                                  relief="flat", command=lambda: on_play(symbol))
        self.btn_play.pack(side="left", padx=1)

        self.btn_stop = tk.Button(self.frame, text="■", width=3,
                                  bg=BTN_DIS_BG, fg=BTN_DIS_FG, font=small_font,
                                  relief="flat", command=lambda: on_stop(symbol))
        self.btn_stop.pack(side="left", padx=1)

        self.btn_opt = tk.Button(self.frame, text="OPT", width=4,
                                 bg=BTN_DIS_BG, fg=BTN_DIS_FG, font=small_font,
                                 relief="flat", command=lambda: on_opt(symbol))
        self.btn_opt.pack(side="left", padx=(1, 4))

    def _set_btn(self, btn, enabled, active_bg, active_fg):
        if enabled:
            btn.config(bg=active_bg, fg=active_fg, state="normal")
        else:
            btn.config(bg=BTN_DIS_BG, fg=BTN_DIS_FG, state="disabled")

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
        for lbl in self.labels:
            lbl.config(bg=bg)

        # ── Offline: minden gomb disabled, de az OPTIMIZING/QUEUED állapot látható ─
        if not connected and inst_state not in ("OPTIMIZING", "QUEUED"):
            self.labels[0].config(text=self.symbol, fg=FG_GRAY_DIM,
                                  font=("Courier", 9, "italic"))
            for lbl in self.labels[1:]:
                lbl.config(text="—", fg=FG_GRAY_DIM)
            self._set_btn(self.btn_play, False, BTN_PLAY_BG, BTN_PLAY_FG)
            self._set_btn(self.btn_stop, False, BTN_STOP_BG, BTN_STOP_FG)
            self._set_btn(self.btn_opt,  False, BTN_OPT_BG,  BTN_OPT_FG)
            return

        if not trained and inst_state not in ("OPTIMIZING", "QUEUED"):
            self.labels[0].config(text=self.symbol, fg=FG_GRAY_DIM,
                                  font=("Courier", 9, "italic"))
            for lbl in self.labels[1:]:
                lbl.config(text="—", fg=FG_GRAY_DIM)
            self._set_btn(self.btn_play, False, BTN_PLAY_BG, BTN_PLAY_FG)
            self._set_btn(self.btn_stop, False, BTN_STOP_BG, BTN_STOP_FG)
            self._set_btn(self.btn_opt,  True,  BTN_OPT_BG,  BTN_OPT_FG)
            return

        if inst_state in ("OPTIMIZING", "QUEUED"):
            self.labels[0].config(text=self.symbol, fg=FG_YELLOW,
                                  font=("Courier", 9, "bold"))
            for lbl in self.labels[1:-1]:
                lbl.config(text="—", fg=FG_GRAY_DIM)
            opt_fg = FG_YELLOW if inst_state == "OPTIMIZING" else FG_GRAY
            self.labels[-1].config(text=opt_status or "—", fg=opt_fg)
            self._set_btn(self.btn_play, False, BTN_PLAY_BG, BTN_PLAY_FG)
            self._set_btn(self.btn_stop, False, BTN_STOP_BG, BTN_STOP_FG)
            self._set_btn(self.btn_opt,  False, BTN_OPT_BG,  BTN_OPT_FG)
            return

        if inst_state == "STOPPED":
            self.labels[0].config(text=self.symbol, fg=FG_GRAY,
                                  font=("Courier", 9, "normal"))
            for lbl in self.labels[1:-1]:
                lbl.config(text="—", fg=FG_GRAY)
            opt_txt = opt_status if opt_status else "—"
            self.labels[-1].config(text=opt_txt,
                                   fg=FG_GREEN if "Kész" in opt_txt else FG_GRAY)
            self._set_btn(self.btn_play, trained, BTN_PLAY_BG, BTN_PLAY_FG)
            self._set_btn(self.btn_stop, False,   BTN_STOP_BG, BTN_STOP_FG)
            self._set_btn(self.btn_opt,  True,    BTN_OPT_BG,  BTN_OPT_FG)
            return

        # LIVE
        self.labels[0].config(text=self.symbol, fg=FG_WHITE,
                               font=("Courier", 9, "bold"))
        sma = ds.sma_direction
        self.labels[1].config(
            text=sma if sma != "NONE" else "—",
            fg=FG_GREEN if sma == "BUY" else (FG_RED if sma == "SELL" else FG_GRAY))
        self.labels[2].config(text=f"{ds.wpr_m15:.1f}",
                               fg=FG_YELLOW if ds.m15_signal != "—" else FG_WHITE)
        m15_sig = ds.m15_signal
        self.labels[3].config(text=m15_sig,
                               fg=FG_GREEN if "BUY" in m15_sig else
                               (FG_RED if "SELL" in m15_sig else FG_GRAY))
        m15r = ds.m15_remaining_s
        self.labels[4].config(text=f"{m15r//60}:{m15r%60:02d}", fg=FG_GRAY)
        self.labels[5].config(text=f"{ds.wpr_m1:.1f}",
                               fg=FG_YELLOW if ds.m1_signal != "—" else FG_WHITE)
        m1_sig = ds.m1_signal
        self.labels[6].config(text=m1_sig,
                               fg=FG_GREEN if "BUY" in m1_sig else
                               (FG_RED if "SELL" in m1_sig else FG_GRAY))
        m1r = ds.m1_remaining_s
        self.labels[7].config(text=f"{m1r//60}:{m1r%60:02d}", fg=FG_GRAY)
        sp     = getattr(ds, "spread_pts", 0)
        sp_max = getattr(ds, "max_spread_pts", 0)
        if sp_max > 0:
            sp_txt = f"{sp}/{sp_max}"
            sp_fg  = FG_RED if sp > sp_max else FG_GREEN
        else:
            sp_txt = f"{sp}" if sp > 0 else "—"
            sp_fg  = FG_GRAY
        self.labels[8].config(text=sp_txt, fg=sp_fg)
        if ds.position_pnl is not None:
            pnl_str = f"{ds.position_pnl:+.2f}$" + (" ✦" if ds.risk_free else "")
            self.labels[9].config(text=pnl_str,
                                   fg=FG_GREEN if ds.position_pnl >= 0 else FG_RED)
        else:
            self.labels[9].config(text="—", fg=FG_GRAY)
        dpnl = ds.daily_pnl
        self.labels[10].config(text=f"{dpnl:+.2f}$",
                                fg=FG_GREEN if dpnl >= 0 else FG_RED)
        opt_txt = opt_status if opt_status else "—"
        self.labels[11].config(text=opt_txt,
                                fg=FG_GREEN if "Kész" in opt_txt else FG_GRAY)
        self._set_btn(self.btn_play, False, BTN_PLAY_BG, BTN_PLAY_FG)
        self._set_btn(self.btn_stop, not has_position, BTN_STOP_BG, BTN_STOP_FG)
        self._set_btn(self.btn_opt,  False, BTN_OPT_BG,  BTN_OPT_FG)


# ---------------------------------------------------------------------------
# Live Dashboard — fejléc sor
# ---------------------------------------------------------------------------

class HeaderRow:
    def __init__(self, parent: tk.Frame, header_font, small_font, on_col_click=None):
        self.frame = tk.Frame(parent, bg=BG_HEADER)
        self.frame.pack(fill="x", padx=2, pady=(4, 0))
        self._lbls: list[tk.Label] = []
        for i, (col_name, width, anchor) in enumerate(COLUMNS):
            lbl = tk.Label(
                self.frame, text=col_name, width=width, anchor=anchor,
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
            col_name = COLUMNS[i][0]
            col_w    = COLUMNS[i][1]
            if i == col_idx and direction != 0:
                arrow  = "▲" if direction == 1 else "▼"
                # arrow beillesztése a width-en belül — ne tágítsa a labelt
                full   = f"{col_name} {arrow}"
                lbl.config(fg=FG_CYAN, text=full, width=col_w)
            else:
                lbl.config(fg=FG_BLUE, text=col_name, width=col_w)


# ---------------------------------------------------------------------------
# Optimizer vezérlő
# ---------------------------------------------------------------------------

class OptimizerController:
    def __init__(self, cfg: dict, instrument_state: dict, optimizer_status: dict,
                 max_parallel: int = 2):
        self.cfg              = cfg
        self.instrument_state = instrument_state
        self.optimizer_status = optimizer_status
        self.max_parallel     = max_parallel
        self._lock            = threading.Lock()
        self._queue: list     = []
        self._running: set    = set()

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

    def _start(self, symbol: str):
        self._running.add(symbol)
        self.instrument_state[symbol] = "OPTIMIZING"
        self.optimizer_status[symbol] = "Indul..."
        threading.Thread(target=self._run_worker, args=(symbol,), daemon=True).start()

    def _run_worker(self, symbol: str):
        try:
            from ml.optimizer import (
                generate_random_params, generate_grid_params,
                optimize_pair, params_file,
            )
            from trading.backtest import load_data, run_pair

            opt_cfg     = self.cfg["optimizer"]
            method      = opt_cfg.get("method", "random")
            max_trials  = opt_cfg.get("max_trials", 500)
            train_start = opt_cfg.get("train_start_date", "2025-01-01")
            test_start  = opt_cfg.get("test_start_date", "2025-10-01")
            initial_bal = self.cfg.get("ml", {}).get("starting_balance_eur", 1000.0)
            trading_cfg = self.cfg["trading"]
            pair_cfg    = self.cfg["pairs"][symbol]
            base_params = {**self.cfg["indicators"], **self.cfg["sltp"],
                           **self.cfg["position_mgmt"]}

            # ── Adat előkészítés ──────────────────────────────────────────
            # Ha létezik a fájl: gap-fill (gyors, csak a hiányzó bars).
            # Ha nem létezik:    teljes letöltés bar-szinten (háttérszálban,
            #                    MT5_LOCK alatt — GUI nem fagy).
            from core.mt5_connector import MT5_LOCK
            from tools.download_history import download_pair, _fill_gap
            from datetime import datetime as _dt, timezone as _tz
            import MetaTrader5 as _mt5_dl

            end_dt = _dt.now(_tz.utc)
            with MT5_LOCK:
                connected = _mt5_dl.initialize()

            if connected:
                for tf in ("M15", "M1"):
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

            import pandas as pd
            df_m15, df_m1 = load_data(symbol)
            if df_m15 is None:
                self.optimizer_status[symbol] = "Hiba: nincs adat"
                return

            df_m15 = df_m15[df_m15.index >= train_start].copy()
            df_m1  = df_m1[df_m1.index  >= train_start].copy()

            if method == "grid":
                params_list = generate_grid_params(opt_cfg, base_params)
            else:
                params_list = generate_random_params(opt_cfg, base_params, max_trials)

            total = len(params_list)
            self.optimizer_status[symbol] = f"0/{total}  0%"

            def progress(done, tot, best_pnl):
                pct = int(done / tot * 100)
                self.optimizer_status[symbol] = f"{done}/{tot}  {pct}%"

            result = optimize_pair(
                symbol, df_m15, df_m1, params_list, pair_cfg, trading_cfg,
                initial_bal, test_start, progress_callback=progress,
            )

            if result is None:
                self.optimizer_status[symbol] = "Hiba: nincs eredmény"
                return

            test_result  = run_pair(symbol, df_m15, df_m1,
                                    result["params"], pair_cfg, trading_cfg,
                                    initial_bal, test_start=test_start)
            test_summary = test_result.summary(initial_bal)

            from datetime import datetime as _dt2
            entry = {
                "symbol":        symbol,
                "optimized_at":  _dt2.utcnow().isoformat(),
                "train_summary": result["train_summary"],
                "test_summary":  test_summary,
                "params":        result["params"],
            }
            out = params_file(symbol)
            tmp = out.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(entry, f, indent=2, ensure_ascii=False, default=str)
            tmp.replace(out)
            self.optimizer_status[symbol] = "Kész ✓"

        except Exception as e:
            import traceback, logging as _logging
            tb = traceback.format_exc()
            _logging.getLogger(__name__).error("OPT hiba [%s]: %s\n%s", symbol, e, tb)
            try:
                err_file = ROOT / "data" / "opt_error.log"
                with open(err_file, "a", encoding="utf-8") as _ef:
                    from datetime import datetime as _dt2
                    _ef.write(f"\n{'='*60}\n{_dt2.now()} [{symbol}]\n{tb}\n")
            except Exception:
                pass
            self.optimizer_status[symbol] = f"Hiba: {e}"
        finally:
            with self._lock:
                self._running.discard(symbol)
                self.instrument_state[symbol] = "STOPPED"
                self._try_start_next()

    def _try_start_next(self):
        while self._queue and len(self._running) < self.max_parallel:
            nxt = self._queue.pop(0)
            if self.instrument_state.get(nxt) == "QUEUED":
                self._start(nxt)


# ---------------------------------------------------------------------------
# Portfólió Backtest Tab
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

        # ── Felső sáv: kontrollok + progress ──────────────────────────────
        top = tk.Frame(p, bg=BG_BT)
        top.pack(fill="x", padx=8, pady=6)

        # Bal: instrumentum választó + dátumok
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

        # Jobb: progress + equity canvas
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

        # Progress bar
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("BT.Horizontal.TProgressbar",
                        troughcolor=BG_HEADER, background=BTN_BT_BG,
                        thickness=8)
        self._progressbar = ttk.Progressbar(right, style="BT.Horizontal.TProgressbar",
                                            orient="horizontal", length=400,
                                            mode="determinate", maximum=100)
        self._progressbar.pack(fill="x", pady=(4, 6))

        # Equity görbe canvas
        tk.Label(right, text="Equity görbe:", bg=BG_BT, fg=FG_GRAY,
                 font=self._small).pack(anchor="w")
        self._canvas = tk.Canvas(right, height=140, bg=CANVAS_BG,
                                 highlightthickness=0)
        self._canvas.pack(fill="x", pady=(0, 6))

        # ── Elválasztó ────────────────────────────────────────────────────
        tk.Frame(p, bg=FG_GRAY_DIM, height=1).pack(fill="x", padx=4, pady=2)

        # ── Eredménytáblázat ──────────────────────────────────────────────
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

    # ── Backtest indítás / leállítás ──────────────────────────────────────

    def _start_bt(self):
        if self._thread and self._thread.is_alive():
            return

        symbols = [s for s, v in self._sym_vars.items() if v.get()]
        if not symbols:
            self._lbl_status.config(text="Válassz legalább egy instrumentumot!",
                                    fg=FG_RED)
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
                "running":   True,
                "date":      date_str,
                "balance":   balance,
                "n_open":    n_open,
                "n_closed":  n_closed,
                "pct":       pct,
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
            # Frissítjük a UI-t
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
            self._lbl_pnl.config(
                text=f"P&L: {pnl:+.2f}$ ({pnl_pct:+.1f}%)", fg=pnl_fg)
            self._lbl_trades.config(
                text=f"Lezárt: {n_closed}   Nyitott: {n_open}")
            self._progressbar["value"] = pct

            self._draw_equity(self._equity_pts, init_bal)
            self.parent.after(300, self._poll_progress)
        else:
            # Kész
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
                    text=f"Kész! {n} trade | P&L: {pnl:+.2f}$ "
                         f"({pnl/init_bal*100:+.1f}%)",
                    fg=FG_GREEN if pnl >= 0 else FG_RED)
                self._show_results(result)
                self._draw_equity(result.get("equity_curve", []), init_bal)
            else:
                self._lbl_status.config(text="Leállítva.", fg=FG_GRAY)

    # ── Equity görbe ──────────────────────────────────────────────────────

    def _draw_equity(self, points: list, init_bal: float = 1000.0):
        c = self._canvas
        c.delete("all")
        w = c.winfo_width() or 500
        h = c.winfo_height() or 140
        pad = 8

        if not points or len(points) < 2:
            c.create_text(w // 2, h // 2, text="Nincs adat",
                          fill=FG_GRAY, font=("Courier", 9))
            return

        balances = [b for _, b in points]
        mn = min(balances + [init_bal])
        mx = max(balances + [init_bal])
        rng = mx - mn or 1

        def px(i):
            return pad + (i / (len(points) - 1)) * (w - 2 * pad)
        def py(b):
            return h - pad - ((b - mn) / rng) * (h - 2 * pad)

        # Referencia vonal (kezdő tőke)
        ref_y = py(init_bal)
        c.create_line(pad, ref_y, w - pad, ref_y,
                      fill=CANVAS_REF, dash=(4, 4), width=1)

        # Equity görbe
        coords = []
        for i, (_, b) in enumerate(points):
            coords += [px(i), py(b)]
        if len(coords) >= 4:
            final_bal = balances[-1]
            color = CANVAS_LINE if final_bal >= init_bal else FG_RED
            c.create_line(*coords, fill=color, width=2, smooth=True)

        # Feliratok
        c.create_text(pad + 2, h - pad - 2,
                      text=f"${mn:.0f}", fill=FG_GRAY,
                      font=("Courier", 7), anchor="sw")
        c.create_text(pad + 2, pad + 2,
                      text=f"${mx:.0f}", fill=FG_GRAY,
                      font=("Courier", 7), anchor="nw")
        if points:
            c.create_text(w - pad, h - pad - 2,
                          text=str(points[-1][0])[:7], fill=FG_GRAY,
                          font=("Courier", 7), anchor="se")
            c.create_text(pad, h - pad - 2,
                          text=str(points[0][0])[:7], fill=FG_GRAY,
                          font=("Courier", 7), anchor="sw")

    # ── Eredménytáblázat ──────────────────────────────────────────────────

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
            tt  = by_sym[sym]
            pnl = s.get("total_pnl", 0)
            pf  = s.get("profit_factor", 0)
            pf_str = f"{min(pf, 99):.2f}" if pf != float("inf") else "∞"
            # Páronkénti végegyenleg közelítés
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
                         bg=bg, fg=fg, font=self._small,
                         padx=4, pady=2).pack(side="left")
            row_idx += 1

        # Összesített sor
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
# Fő Dashboard ablak (ttk.Notebook-kal)
# ---------------------------------------------------------------------------

class DashboardWindow:
    def __init__(self, cfg: dict, dashboard_ref: dict,
                 instrument_state: dict, optimizer_status: dict,
                 on_play_pair, on_stop_pair):
        self.cfg              = cfg
        self.dashboard_ref    = dashboard_ref
        self.instrument_state = instrument_state
        self.optimizer_status = optimizer_status
        self._on_play         = on_play_pair
        self._on_stop         = on_stop_pair

        max_par = cfg.get("optimizer", {}).get("max_parallel_optimizers", 2)
        self._opt_ctrl = OptimizerController(
            cfg, instrument_state, optimizer_status, max_parallel=max_par)

        self.root = tk.Tk()
        self.root.title("MT5 Erik — Live Dashboard")
        self.root.configure(bg=BG)
        self.root.resizable(True, True)

        mono_font   = tkfont.Font(family="Courier New", size=9)
        header_font = tkfont.Font(family="Courier New", size=9, weight="bold")
        small_font  = tkfont.Font(family="Courier New", size=8)
        title_font  = tkfont.Font(family="Courier New", size=10, weight="bold")
        info_font   = tkfont.Font(family="Courier New", size=9)

        # ── Globális fejléc — 1. sor: cím + kapcsolat + idő ──────────────
        top_bar = tk.Frame(self.root, bg=BG_HEADER, pady=5)
        top_bar.pack(fill="x", padx=4, pady=(4, 0))

        tk.Label(top_bar, text="MT5 Erik — Dashboard",
                 bg=BG_HEADER, fg=FG_BLUE, font=title_font).pack(side="left", padx=10)

        # Kapcsolat státusz (jobb oldal, majd idő)
        self.lbl_time = tk.Label(top_bar, text="", bg=BG_HEADER,
                                 fg=FG_GRAY, font=info_font)
        self.lbl_time.pack(side="right", padx=10)

        self._btn_connect = tk.Button(
            top_bar, text="⟳  Kapcsolódás", font=small_font,
            bg=BTN_OPT_BG, fg=BTN_OPT_FG, relief="flat",
            command=self._handle_connect,
        )
        # alapból rejtve, csak offline módban jelenik meg
        self._btn_connect.pack(side="right", padx=6)
        self._btn_connect.pack_forget()

        self.lbl_conn = tk.Label(top_bar, text="● Offline",
                                 bg=BG_HEADER, fg=FG_RED, font=info_font)
        self.lbl_conn.pack(side="right", padx=(0, 4))

        self.lbl_account = tk.Label(top_bar, text="", bg=BG_HEADER,
                                    fg=FG_GRAY, font=info_font)
        self.lbl_account.pack(side="right", padx=10)

        # ── 2. sor: kereskedési adatok ────────────────────────────────────
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

        # ── Notebook (fülek) ──────────────────────────────────────────────
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TNotebook",        background=BG, borderwidth=0)
        style.configure("TNotebook.Tab",
                        background=BG_HEADER, foreground=FG_GRAY,
                        padding=[12, 4], font=("Courier New", 9))
        style.map("TNotebook.Tab",
                  background=[("selected", BG)],
                  foreground=[("selected", FG_BLUE)])

        self._notebook = ttk.Notebook(self.root)
        self._notebook.pack(fill="both", expand=True, padx=2)

        # ── Fül 1: Live Dashboard ──────────────────────────────────────────
        live_frame = tk.Frame(self._notebook, bg=BG)
        self._notebook.add(live_frame, text="  Live Dashboard  ")
        self._build_live_tab(live_frame, mono_font, header_font, small_font)

        # ── Fül 2: Portfólió Backtest ─────────────────────────────────────
        bt_frame = tk.Frame(self._notebook, bg=BG_BT)
        self._notebook.add(bt_frame, text="  Portfólió Backtest  ")
        self._bt_tab = PortfolioBacktestTab(
            bt_frame, cfg, mono_font, small_font, header_font)

        self._balance    = 0.0
        self._free_slots = cfg["trading"]["max_open_slots"]
        self._max_slots  = cfg["trading"]["max_open_slots"]

        self._refresh()

    def _build_live_tab(self, parent, mono_font, header_font, small_font):
        self._mono_font   = mono_font
        self._small_font  = small_font
        self._header_font = header_font
        self._sort_col    = None   # None = nincs rendezés
        self._sort_dir    = 1      # 1=ASC, -1=DESC

        # ── Toolbar: keresés + szűrők + + gomb ───────────────────────────
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
        tk.Checkbutton(toolbar, text="STOPPED elrejtése",
                       variable=self._hide_stopped_var,
                       bg=BG, fg=FG_GRAY, selectcolor=BG_HEADER,
                       activebackground=BG, activeforeground=FG_WHITE,
                       font=small_font,
                       command=self._apply_filter_sort).pack(side="left", padx=4)

        tk.Button(toolbar, text="  +  Instrumentum", font=small_font,
                  bg=BTN_OPT_BG, fg=BTN_OPT_FG, relief="flat", cursor="hand2",
                  command=self._show_add_instrument).pack(side="right", padx=4)

        # ── Legenda ────────────────────────────────────────────────────────
        legend = tk.Frame(parent, bg=BG, pady=2)
        legend.pack(fill="x", padx=6)
        for text, color in [
            ("■ LIVE", FG_GREEN), ("■ STOPPED", FG_GRAY),
            ("■ Nem tanított", FG_GRAY_DIM),
            ("■ Optimalizálás", FG_YELLOW), ("✦ Kockázatmentes", FG_CYAN),
        ]:
            tk.Label(legend, text=text, bg=BG, fg=color,
                     font=small_font, padx=6).pack(side="left")
        tk.Frame(parent, bg=FG_GRAY_DIM, height=1).pack(fill="x", padx=2, pady=2)

        # ── Rendezható fejléc ──────────────────────────────────────────────
        self._table_frame = tk.Frame(parent, bg=BG)
        self._table_frame.pack(fill="both", expand=True, padx=2)

        self._header_row = HeaderRow(
            self._table_frame, header_font, small_font,
            on_col_click=self._on_header_click,
        )

        # ── Sorok létrehozása (nem pack-elve még) ──────────────────────────
        self.rows: dict[str, PairRow] = {}
        for idx, (symbol, pair_cfg) in enumerate(self.cfg["pairs"].items()):
            if not isinstance(pair_cfg, dict):
                continue
            row = PairRow(
                self._table_frame, symbol, idx,
                on_play=self._handle_play,
                on_stop=self._handle_stop,
                on_opt=self._handle_opt,
                mono_font=mono_font,
                small_font=small_font,
            )
            self.rows[symbol] = row

        self._apply_filter_sort()

        tk.Frame(parent, bg=FG_GRAY_DIM, height=1).pack(fill="x", padx=2, pady=2)
        self.lbl_status = tk.Label(parent, text="Indulás...",
                                   bg=BG, fg=FG_GRAY, font=small_font)
        self.lbl_status.pack(side="bottom", pady=4)

    def _on_header_click(self, col_idx: int):
        if self._sort_col == col_idx:
            if self._sort_dir == 1:
                self._sort_dir = -1
            else:
                self._sort_col = None   # harmadik kattintás: rendezés törlése
                self._sort_dir = 1
        else:
            self._sort_col = col_idx
            self._sort_dir = 1
        self._header_row.set_sort(self._sort_col, self._sort_dir)
        self._apply_filter_sort()

    def _sort_key(self, symbol: str):
        col = self._sort_col
        ds  = self.dashboard_ref.get(symbol)
        st  = self.instrument_state.get(symbol, "STOPPED")
        if col == 0:   # Instrumentum
            return symbol
        if col == 1:   # SMA irány
            return {"BUY": 0, "SELL": 1, "—": 2}.get(
                getattr(ds, "sma_direction", "—"), 2)
        if col == 2:   # M15 WPR
            return getattr(ds, "wpr_m15", 0.0)
        if col == 5:   # M1 WPR
            return getattr(ds, "wpr_m1", 0.0)
        if col == 8:   # Spread
            return getattr(ds, "spread_pts", 0)
        if col == 9:   # Pozíció P&L
            return getattr(ds, "position_pnl", None) or 0.0
        if col == 10:  # Napi P&L
            return getattr(ds, "daily_pnl", 0.0)
        if col == 11:  # Opt státusz
            return self.optimizer_status.get(symbol, "")
        return symbol

    def _apply_filter_sort(self):
        search       = self._search_var.get().upper().strip() \
                       if hasattr(self, "_search_var") else ""
        hide_stopped = self._hide_stopped_var.get() \
                       if hasattr(self, "_hide_stopped_var") else False

        # Szűrés
        visible = []
        for symbol in self.rows:
            if search and search not in symbol.upper():
                continue
            st = self.instrument_state.get(symbol, "STOPPED")
            if hide_stopped and st == "STOPPED":
                continue
            visible.append(symbol)

        # Rendezés
        if self._sort_col is not None:
            visible.sort(key=self._sort_key, reverse=(self._sort_dir == -1))

        # Újra-csomagolás
        for sym in self.rows:
            self.rows[sym].frame.pack_forget()
        for sym in visible:
            self.rows[sym].frame.pack(fill="x", padx=2, pady=0)

    def _show_add_instrument(self):
        """Popup: MT5 szimbólumok amik még nincsenek a config-ban."""
        popup = tk.Toplevel(self.root)
        popup.title("Instrumentum hozzáadása")
        popup.configure(bg=BG)
        popup.resizable(False, False)
        popup.grab_set()

        tk.Label(popup, text="Elérhető szimbólumok (MT5):",
                 bg=BG, fg=FG_BLUE,
                 font=self._header_font).pack(padx=12, pady=(10, 4), anchor="w")

        # Keresés
        search_var = tk.StringVar()
        tk.Entry(popup, textvariable=search_var, width=28,
                 bg=BG_HEADER, fg=FG_WHITE, font=self._small_font,
                 insertbackground=FG_WHITE, relief="flat").pack(padx=12, pady=(0, 6))

        # MT5 szimbólumok lekérése
        in_config = set(self.rows.keys())
        try:
            import MetaTrader5 as mt5
            syms = mt5.symbols_get()
            all_syms = sorted(s.name for s in syms) if syms else []
        except Exception:
            all_syms = []
        available = [s for s in all_syms if s not in in_config]

        # Listbox + scrollbar
        frame_lb = tk.Frame(popup, bg=BG)
        frame_lb.pack(padx=12, fill="both", expand=True)
        scrollbar = tk.Scrollbar(frame_lb)
        scrollbar.pack(side="right", fill="y")
        listbox = tk.Listbox(frame_lb, width=30, height=18,
                             bg=BG_HEADER, fg=FG_WHITE,
                             selectbackground=BTN_OPT_BG,
                             font=self._small_font, relief="flat",
                             yscrollcommand=scrollbar.set)
        listbox.pack(side="left", fill="both", expand=True)
        scrollbar.config(command=listbox.yview)

        def refresh_list(*_):
            q = search_var.get().upper()
            listbox.delete(0, "end")
            for s in available:
                if q in s.upper():
                    listbox.insert("end", s)
        search_var.trace_add("write", refresh_list)
        refresh_list()

        lbl_info = tk.Label(popup, text="", bg=BG, fg=FG_GRAY,
                            font=self._small_font)
        lbl_info.pack(pady=(4, 0))
        if not available:
            lbl_info.config(text="Minden MT5 szimbólum már szerepel a listában.",
                            fg=FG_YELLOW)

        def add_selected():
            sel = listbox.curselection()
            if not sel:
                return
            symbol = listbox.get(sel[0])
            self._add_instrument(symbol)
            popup.destroy()

        btn_frame = tk.Frame(popup, bg=BG)
        btn_frame.pack(pady=10)
        tk.Button(btn_frame, text="Hozzáadás", bg=BTN_PLAY_BG, fg=BTN_PLAY_FG,
                  font=self._small_font, relief="flat",
                  command=add_selected).pack(side="left", padx=6)
        tk.Button(btn_frame, text="Mégse", bg=BTN_DIS_BG, fg=BTN_DIS_FG,
                  font=self._small_font, relief="flat",
                  command=popup.destroy).pack(side="left", padx=6)

        # Dupla kattintás = azonnali hozzáadás
        listbox.bind("<Double-Button-1>", lambda _: add_selected())

    def _add_instrument(self, symbol: str):
        """Új instrumentum hozzáadása a config-hoz és a dashboard sorhoz."""
        if symbol in self.rows:
            return

        # MT5-ből lekérjük az alap adatokat
        pip_size = 0.0001
        pv1_usd  = 10.0
        spread_pips = 1.5
        try:
            import MetaTrader5 as _mt5
            from core.mt5_connector import MT5_LOCK
            with MT5_LOCK:
                info = _mt5.symbol_info(symbol)
            if info:
                # Forex 4/5 tizedesjegy: pip = point×10
                # Forex 2/3 tizedesjegy (JPY): pip = point×100
                # Index/Crypto/Áru: pip = point (1 tizedesjegy vagy egész)
                d = info.digits
                if d in (4, 5):
                    pip_size = info.point * 10
                elif d in (2, 3):
                    pip_size = info.point * 100
                else:
                    pip_size = info.point  # index, crypto, áru
                tv = info.trade_tick_value
                ts = info.trade_tick_size
                pv1_usd = round(tv / ts * pip_size, 4) if ts > 0 else tv
                # spread becslés pontból
                spread_pips = round(info.spread * info.point / pip_size, 1) \
                              if pip_size > 0 else 1.5
        except Exception:
            pass

        # Config frissítése
        self.cfg["pairs"][symbol] = {
            "enabled":              False,
            "pip_size":             pip_size,
            "pv1_usd":              pv1_usd,
            "backtest_spread_pips": spread_pips,
            "sess_start":           0,
            "sess_end":             24,
        }
        try:
            cfg_path = ROOT / "config.json"
            import json as _json
            with open(cfg_path, "w", encoding="utf-8") as f:
                _json.dump(self.cfg, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

        # Dashboard state
        from trading.live_trader import PairDashboardState
        self.dashboard_ref[symbol] = PairDashboardState(
            symbol=symbol, trained=False, enabled=False)
        self.instrument_state[symbol] = "STOPPED"
        self.optimizer_status[symbol] = ""

        # Új sor a táblázatban
        idx = len(self.rows)
        row = PairRow(
            self._table_frame, symbol, idx,
            on_play=self._handle_play,
            on_stop=self._handle_stop,
            on_opt=self._handle_opt,
            mono_font=self._mono_font,
            small_font=self._small_font,
        )
        self.rows[symbol] = row
        self._apply_filter_sort()

    # ── Gomb handlerek ────────────────────────────────────────────────────

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
        if self.instrument_state.get(symbol) != "STOPPED":
            return
        self._opt_ctrl.request_optimize(symbol)
        # Azonnal frissítjük a sort vizuálisan — ne várjunk 1 mp-et a _refresh-re
        row = self.rows.get(symbol)
        ds  = self.dashboard_ref.get(symbol)
        if row and ds:
            new_state = self.instrument_state.get(symbol, "STOPPED")
            opt_txt   = self.optimizer_status.get(symbol, "Indul...")
            row.update(ds, new_state, opt_txt,
                       connected=getattr(self, "_connected", True))

    def _handle_connect(self):
        """Connect gomb — megpróbál újra kapcsolódni MT5-höz."""
        try:
            from core import mt5_connector
            if mt5_connector.connect(self.cfg):
                info = mt5_connector.connection_info(self.cfg)
                self._update_connection_ui(info)
        except Exception as e:
            self.lbl_conn.config(text=f"● Hiba: {e}", fg=FG_RED)

    # ── Publikus API ──────────────────────────────────────────────────────

    def set_balance(self, balance: float):
        self._balance = balance

    def set_slots(self, free: int, max_s: int):
        self._free_slots = free
        self._max_slots  = max_s

    # ── Frissítés ─────────────────────────────────────────────────────────

    def _update_connection_ui(self, info: dict):
        """Kapcsolat UI frissítése egy connection_info dict alapján."""
        self._connected = info.get("connected", False)
        if info["connected"]:
            demo_tag = "  [DEMO]" if info.get("is_demo") else "  [ÉLES!]"
            demo_fg  = FG_YELLOW if info.get("is_demo") else FG_RED
            self.lbl_conn.config(text="● Online", fg=FG_GREEN)
            self.lbl_account.config(
                text=f"#{info['login']}  {info['server']}{demo_tag}",
                fg=demo_fg,
            )
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
                fg=FG_GRAY,
            )
            self._btn_connect.pack(side="right", padx=6)

    def _start_market_data_poll(self):
        """Háttérszál: 30 mp-enként lekéri az indikátor értékeket MT5-ből."""
        if not hasattr(self, "_poll_running"):
            self._poll_running = True
            threading.Thread(target=self._market_data_loop, daemon=True).start()

    def _market_data_loop(self):
        import time as _time
        _time.sleep(5)   # UI stabilizálódjon indítás után
        while getattr(self, "_poll_running", False):
            # Ne fusson MT5 lekérés ha optimizer aktív (MT5 nem thread-safe)
            if not self._opt_ctrl._running:
                try:
                    self._fetch_market_data()
                except Exception:
                    pass
            _time.sleep(30)

    def _fetch_market_data(self):
        """MT5-ből lekéri az aktuális M15/M1 indikátor értékeket minden párhoz."""
        try:
            import MetaTrader5 as _mt5
            from core.mt5_connector import MT5_LOCK
        except Exception:
            return

        from core.indicator_engine import compute_indicators
        from core.signal_detector import check_m15_signal, check_m1_entry, PairState
        from ml.optimizer import PARAMS_DIR

        for symbol, ds in self.dashboard_ref.items():
            if not isinstance(self.cfg["pairs"].get(symbol), dict):
                continue

            # Optimalizált params betöltése (ha van)
            params_f = PARAMS_DIR / f"{symbol}.json"
            if params_f.exists():
                with open(params_f, encoding="utf-8") as f:
                    import json as _json
                    data = _json.load(f)
                params = data.get("params", {})
            else:
                params = {**self.cfg.get("indicators", {}),
                          **self.cfg.get("sltp", {}),
                          **self.cfg.get("position_mgmt", {})}
                if not params.get("sma_period"):
                    continue

            pair_cfg = self.cfg["pairs"][symbol]
            warmup   = max(params.get("sma_period", 200),
                           params.get("wpr_m15_period", 21),
                           params.get("atr_period", 14)) + 5
            warmup_m1 = params.get("wpr_m1_period", 8) + 5

            with MT5_LOCK:
                bars_m15 = _mt5.copy_rates_from_pos(symbol, _mt5.TIMEFRAME_M15, 0, warmup)
                bars_m1  = _mt5.copy_rates_from_pos(symbol, _mt5.TIMEFRAME_M1,  0, warmup_m1)
            if bars_m15 is None or bars_m1 is None:
                continue

            import pandas as _pd
            df_m15 = _pd.DataFrame(bars_m15)
            df_m1  = _pd.DataFrame(bars_m1)
            for df in (df_m15, df_m1):
                df["time"] = _pd.to_datetime(df["time"], unit="s", utc=True)
                df.set_index("time", inplace=True)

            try:
                m15, m1 = compute_indicators(df_m15, df_m1, params)
            except Exception:
                continue

            if len(m15) < 2 or len(m1) < 2:
                continue

            m15_closed = m15.iloc[-2]
            m1_closed  = m1.iloc[-2]
            m1_prev    = m1.iloc[-3] if len(m1) >= 3 else m1.iloc[-2]

            import math as _math
            if any(_math.isnan(v) for v in [
                m15_closed.get("sma", float("nan")),
                m15_closed.get("wpr", float("nan")),
                m15_closed.get("atr", float("nan")),
            ]):
                continue

            # State frissítés
            state = PairState(symbol=symbol)
            state = check_m15_signal(
                state,
                close=float(m15_closed["close"]),
                sma=float(m15_closed["sma"]),
                wpr_m15=float(m15_closed["wpr"]),
                params=params,
            )

            ds.sma_direction = state.direction
            ds.wpr_m15       = round(float(m15_closed["wpr"]), 1)
            ds.m15_signal    = (
                f"{state.direction}{'▲' if state.direction == 'BUY' else '▼'}"
                if state.m15_window_open else "—"
            )

            if not _math.isnan(m1_closed.get("wpr", float("nan"))):
                ds.wpr_m1 = round(float(m1_closed["wpr"]), 1)
                if not _math.isnan(m1_prev.get("wpr", float("nan"))):
                    sig = check_m1_entry(state, float(m1_prev["wpr"]),
                                         float(m1_closed["wpr"]), params)
                    ds.m1_signal = (f"{sig}{'▲' if sig == 'BUY' else '▼'}"
                                    if sig != "NONE" else "—")

            # Spread frissítés
            with MT5_LOCK:
                sym_info = _mt5.symbol_info(symbol)
            if sym_info:
                atr_pts = int(float(m15_closed["atr"]) / sym_info.point)
                ratio   = params.get("max_spread_atr_ratio", 0.20)
                ds.spread_pts     = sym_info.spread
                ds.max_spread_pts = max(1, int(atr_pts * ratio))
                ds.trained = params_f.exists()

    def _start_bg_poller(self):
        """Háttérszál: 5 mp-enként lekéri az MT5 kapcsolat/account adatokat.
        A főszál (_refresh) csak ebből a shared dict-ből olvas — soha nem blokkol MT5-re."""
        if hasattr(self, "_bg_poller_running"):
            return
        self._bg_poller_running = True
        self._mt5_cache = {
            "connected": False, "info": {}, "daily_pnl": None, "positions": {}
        }
        def _loop():
            import time as _t
            while getattr(self, "_bg_poller_running", False):
                try:
                    from core.mt5_connector import (
                        connection_info, daily_pnl as _dpnl,
                        open_positions_by_symbol,
                    )
                    info = connection_info(self.cfg)
                    self._mt5_cache["connected"] = info.get("connected", False)
                    self._mt5_cache["info"]      = info
                    if info.get("connected"):
                        self._mt5_cache["daily_pnl"] = _dpnl()
                        self._mt5_cache["positions"] = open_positions_by_symbol()
                    else:
                        self._mt5_cache["daily_pnl"] = None
                        self._mt5_cache["positions"] = {}
                except Exception:
                    pass
                _t.sleep(5)
        threading.Thread(target=_loop, daemon=True, name="MT5BgPoller").start()

    def _refresh(self):
        now = datetime.now(timezone.utc)
        self.lbl_time.config(text=now.strftime("%Y-%m-%d %H:%M:%S UTC"))

        # Countdown frissítés — minden másodpercben, csak Python számolás, nem MT5
        try:
            from trading.live_trader import seconds_to_candle_close
            m15_rem = seconds_to_candle_close(15)
            m1_rem  = seconds_to_candle_close(1)
            for ds in self.dashboard_ref.values():
                if hasattr(ds, "m15_remaining_s"):
                    ds.m15_remaining_s = m15_rem
                    ds.m1_remaining_s  = m1_rem
        except Exception:
            pass

        # Indítjuk a háttér pollereket (egyszer, első tick-nél)
        if not hasattr(self, "_conn_tick"):
            self._conn_tick = 0
            self._start_bg_poller()
            self._start_market_data_poll()
        self._conn_tick += 1

        # MT5 állapot a háttér cache-ből — soha nem blokkolunk
        cache     = getattr(self, "_mt5_cache", {})
        connected = cache.get("connected", False)
        mt5_info  = cache.get("info", {})
        mt5_pnl   = cache.get("daily_pnl", None)
        mt5_positions = cache.get("positions", {})

        # Ha a cache még üres (bg poller nem futott), ne írjuk felül az előző állapotot
        if mt5_info:
            self._update_connection_ui(mt5_info)

        if self._balance > 0:
            cur = mt5_info.get("currency", "")
            self.lbl_balance.config(
                text=f"Egyenleg: {self._balance:,.2f} {cur}".rstrip())

        # Napi P&L
        if mt5_pnl is not None:
            daily_total = mt5_pnl
            pnl_src = ""
        else:
            daily_total = sum(
                ds.daily_pnl for ds in self.dashboard_ref.values()
                if hasattr(ds, "daily_pnl"))
            pnl_src = " (demo)"

        self.lbl_daily.config(
            text=f"Napi P&L: {daily_total:+.2f}${pnl_src}",
            fg=FG_GREEN if daily_total >= 0 else FG_RED)

        free = max(0, self._free_slots)
        self.lbl_slots.config(
            text=f"Szabad slotok: {free}/{self._max_slots}",
            fg=FG_GREEN if free > 0 else FG_RED)

        # Napi limit
        total_daily = sum(
            ds.daily_pnl for ds in self.dashboard_ref.values()
            if hasattr(ds, "daily_pnl"))
        limit_hit = (self._balance > 0 and
                     total_daily <= -(self._balance *
                                      self.cfg["trading"]["daily_loss_limit_pct"]))
        self.lbl_limit.config(
            text="Napi limit: STOP" if limit_hit else "Napi limit: OK",
            fg=FG_RED if limit_hit else FG_GREEN)

        # Szabad slot számolás MT5 pozíciók alapján
        if mt5_positions is not None:
            occupied = len(mt5_positions)
            self._free_slots = max(0, self._max_slots - occupied)
            free = self._free_slots
            self.lbl_slots.config(
                text=f"Szabad slotok: {free}/{self._max_slots}",
                fg=FG_GREEN if free > 0 else FG_RED)

        live_count = 0
        if hasattr(self, "rows"):
            for symbol, row in self.rows.items():
                ds         = self.dashboard_ref.get(symbol)
                inst_state = self.instrument_state.get(symbol, "STOPPED")
                opt_status = self.optimizer_status.get(symbol, "")

                # MT5 valódi pozíció adatok felülírják a demo state-et
                if ds is not None and mt5_positions is not None:
                    pos = mt5_positions.get(symbol)
                    ds.position_pnl = pos["pnl"] if pos else None
                    ds.risk_free    = pos["risk_free"] if pos else False

                if ds is not None:
                    row.update(ds, inst_state, opt_status,
                               connected=getattr(self, "_connected", False))
                if inst_state == "LIVE":
                    live_count += 1

        if hasattr(self, "lbl_status"):
            self.lbl_status.config(
                text=f"Utolsó frissítés: {now.strftime('%H:%M:%S')}  |  "
                     f"LIVE: {live_count}")

        self.root.after(1000, self._refresh)

    def run(self):
        self.root.mainloop()


# ---------------------------------------------------------------------------
# Demo mód
# ---------------------------------------------------------------------------

def _demo_dashboard(cfg: dict):
    """
    Demo mód: az UI layout és state machine bemutatása.
    Pénzügyi adatok (pozíció P&L, napi P&L) szándékosan 0/None —
    ezeket csak az éles live_trader tölti fel valódi MT5 adatokkal.
    WPR/SMA/Spread értékek szimuláltak az oszlopok megjelenítéséhez.
    """
    import random
    from trading.live_trader import PairDashboardState

    params_dir   = ROOT / "data" / "optimized_params"
    real_trained = {f.stem for f in params_dir.glob("*.json")} \
                   if params_dir.exists() else set()

    symbols = [s for s, p in cfg["pairs"].items() if isinstance(p, dict)]

    # Csak valódi optimalizált fájlok számítanak "tanítottnak"
    demo_trained = real_trained

    # Állapotok: csak LIVE / STOPPED — nincs fake OPTIMIZING
    states_pool = ["LIVE"] * 4 + ["STOPPED"] * 6
    random.shuffle(states_pool)

    db: dict         = {}
    inst_state: dict = {}
    opt_status: dict = {}

    for i, symbol in enumerate(symbols):
        trained = symbol in demo_trained
        # Tanítás nélkül csak STOPPED lehet; tanítottnál véletlenszerű
        st      = states_pool[i % len(states_pool)] if trained else "STOPPED"

        inst_state[symbol] = st
        opt_status[symbol] = "Kész ✓" if trained else ""

        sp_pts = random.randint(6, 18)
        sp_max = random.randint(12, 25)

        ds = PairDashboardState(
            symbol=symbol,
            enabled=trained,
            trained=trained,
            sma_direction=random.choice(["BUY", "SELL", "—"]) if st == "LIVE" else "—",
            wpr_m15=round(random.uniform(-95, -5), 1) if st == "LIVE" else 0.0,
            wpr_m1=round(random.uniform(-95, -5), 1) if st == "LIVE" else 0.0,
            m15_signal=random.choice(["BUY▲", "SELL▼", "—"]) if st == "LIVE" else "—",
            m1_signal=random.choice(["BUY▲", "SELL▼", "—"]) if st == "LIVE" else "—",
            m15_remaining_s=random.randint(0, 899),
            m1_remaining_s=random.randint(0, 59),
            position_pnl=None,   # nincs valódi pozíció dashboard módban
            risk_free=False,
            daily_pnl=0.0,       # valódi P&L az MT5-től jön, nem innen
            spread_pts=sp_pts,
            max_spread_pts=sp_max,
        )
        db[symbol] = ds

    # Demo módban nincs nyitott pozíció → minden slot szabad
    return db, inst_state, opt_status, 0


# ---------------------------------------------------------------------------
# Belépési pont (demo)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    cfg_path = ROOT / "config.json"
    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)

    db, inst_state, opt_status, n_pos = _demo_dashboard(cfg)
    win = DashboardWindow(
        cfg, db, inst_state, opt_status,
        on_play_pair=None, on_stop_pair=None,
    )
    max_s = cfg["trading"]["max_open_slots"]
    win.set_balance(1024.50)
    win.set_slots(free=max(0, max_s - n_pos), max_s=max_s)
    win.run()
