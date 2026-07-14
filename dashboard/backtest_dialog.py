"""
Backtest-ablak (B3) — a Stratégia Paraméterek ablak „Backtest" gombja nyitja.

Szabványos, önálló ablak egy paraméterkészlet backtesteléséhez:
  • állítható időszak (kezdő/záró dátum; üresen = a teljes letöltött history),
  • progress bar + százalék a futás közben,
  • élő kijelzés: aktuális szimulált idő, egyenleg, nyitott/lezárt kötések,
    és a ténylegesen alkalmazott kockázati technikák (Felező/Pajzs/Risky) száma,
  • a végén: minősítés + metrikák (Trade·Win·MaxDD·P&L·PF) és egy egyszerű
    egyenleg-görbe (sparkline).

A futás a `trading.backtest.run_pair`-t hívja külön szálon; a `progress_callback`
a fő (UI) szálra marshalol (`after(0, …)`) — az UI SOHA nem blokkol. A végeredményt
egy opcionális `on_result(summary)` visszahívással adja a hívó ablaknak (így a
metrika-sáv és a Mentés is látja a friss eredményt).
"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import ttk

import pandas as pd

from dashboard.theme import (
    BG, BG_HEADER,
    FG_WHITE, FG_GREEN, FG_RED, FG_YELLOW, FG_GRAY, FG_GRAY_DIM,
    BTN_PLAY_BG, BTN_PLAY_FG, BTN_BT_BG, BTN_BT_FG, BTN_DIS_BG, BTN_DIS_FG,
    color as sem_color,
)
from core.quality import metric_colors
from core import rr_state as _rrs
from core import risk_reduction as _rrx

# A technika-kulcsok magyar nevei (a rr_technique / progress tech dict-hez)
_TECH_NAMES = {"shield": "Pajzs", "halving": "Felező", "risky": "Risky"}

# A metrika-sáv egységes sorrendje (mint a Stratégia Paraméterek ablakban)
_METRIC_ORDER = [
    ("Trade ", lambda s: str(int(s.get("trades", 0))), None),
    ("Win ",   lambda s: f"{s.get('win_rate', 0) * 100:.0f}%", "win_rate"),
    ("MaxDD ", lambda s: f"{s.get('max_drawdown', 0) * 100:.1f}%", "max_drawdown"),
    ("P&L ",   lambda s: f"{s.get('total_pnl', 0):+.0f}$", "total_pnl"),
    ("PF ",    lambda s: (f"{s.get('profit_factor', 0):.2f}"
                          if s.get('profit_factor', 0) != float('inf') else "∞"),
     "profit_factor"),
]


class BacktestDialog:
    """Önálló backtest-ablak egy adott paraméterkészlethez."""

    def __init__(self, parent, symbol, cfg, strategy, params, pair_cfg,
                 rr_spec, header_font, small_font, on_result=None,
                 preset_name: str = "Ki"):
        self.parent   = parent
        self.symbol   = symbol
        self.cfg      = cfg
        self.strategy = strategy
        self.params   = dict(params)
        self.pair_cfg = pair_cfg
        self.rr_spec  = rr_spec
        self._hf      = header_font
        self._sf      = small_font
        self._on_result = on_result
        self._preset_name = preset_name

        # A megnyitáskori (fő ablak) rr — ehhez viszonyítunk a visszaíráskor:
        # csak akkor írunk vissza a főképernyőre, ha a Backtest-ablakban ugyanezt
        # az rr-t mértük (különben feltáró futtatás, nem szennyezi a mentendőt).
        self._opened_rr_key = self._rr_key(rr_spec)
        _s0 = rr_spec or {}
        self._init_preset   = _s0.get("preset", _rrx.PRESET_OFF)
        self._init_runner   = _s0.get("runner_stop", _rrx.RUNNER_TRAILING)
        self._init_cautious = bool(_s0.get("cautious", False))

        self._df15 = None
        self._df1  = None
        self._running = False
        self._summary = None
        self._build()
        # Adat betöltése háttérben (a dátum-tartományhoz + a futáshoz cache-elve)
        self._load_data_async()

    # ── rr (kockázatcsökkentés) segédek ──────────────────────────────────────
    @staticmethod
    def _rr_key(spec):
        """A spec összehasonlítható kulcsa (preset, runner, óvatos). OFF/None → ('off',)."""
        if not spec or spec.get("preset", _rrx.PRESET_OFF) == _rrx.PRESET_OFF:
            return ("off",)
        return (spec.get("preset"), spec.get("runner_stop"),
                bool(spec.get("cautious")))

    def _preset_from_name(self, name: str) -> str:
        return {v: k for k, v in _rrs.NAME.items()}.get(name, _rrx.PRESET_OFF)

    def _runner_from_name(self, name: str) -> str:
        return {v: k for k, v in _rrs.RUNNER_NAME.items()}.get(
            name, _rrx.RUNNER_TRAILING)

    def _current_rr_spec(self):
        """Az ablakban BEÁLLÍTOTT rr-spec (feltáró). None, ha 'Ki'."""
        preset = self._preset_from_name(self._rr_name.get())
        if preset == _rrx.PRESET_OFF:
            return None
        return {**_rrx.default_config(), "preset": preset,
                "runner_stop": self._runner_from_name(self._runner_name.get()),
                "cautious": bool(self._cautious_var.get())}

    # ── UI ──────────────────────────────────────────────────────────────────
    def _build(self):
        win = tk.Toplevel(self.parent)
        self.win = win
        # A kockázatcsökkentés már az ablakban választható (lása lentebb), ezért a
        # címben nem ismételjük (a „Backtest (Ki)" korábban félrevezető volt).
        win.title(f"{self.symbol} — Backtest")
        win.configure(bg=BG)

        tk.Label(win, text=f"{self.symbol} — Backtest", bg=BG, fg=FG_WHITE,
                 font=self._hf).pack(anchor="w", padx=12, pady=(12, 2))

        # ── Időszak ─────────────────────────────────────────────────────────
        rng = tk.Frame(win, bg=BG)
        rng.pack(anchor="w", padx=12, pady=(4, 0))
        tk.Label(rng, text="Időszak (YYYY-MM-DD, üresen = teljes):", bg=BG,
                 fg=FG_GRAY, font=self._sf).pack(side="left")
        self._start_var = tk.StringVar()
        self._end_var   = tk.StringVar()
        e1 = tk.Entry(rng, width=12, textvariable=self._start_var, bg=BG_HEADER,
                      fg=FG_WHITE, font=self._sf, insertbackground=FG_WHITE,
                      justify="center")
        e1.pack(side="left", padx=(6, 2))
        tk.Label(rng, text="→", bg=BG, fg=FG_GRAY, font=self._sf).pack(side="left")
        e2 = tk.Entry(rng, width=12, textvariable=self._end_var, bg=BG_HEADER,
                      fg=FG_WHITE, font=self._sf, insertbackground=FG_WHITE,
                      justify="center")
        e2.pack(side="left", padx=(2, 0))
        self._span_lbl = tk.Label(win, text="Adat betöltése…", bg=BG,
                                  fg=FG_GRAY_DIM, font=self._sf)
        self._span_lbl.pack(anchor="w", padx=12, pady=(1, 4))

        # ── Kockázatcsökkentés + runner (FELTÁRÓ — nem ment, nem érinti a live-ot) ─
        # A fő ablak beállításából előtöltve. Szabadon váltogatható több futtatás
        # összevetéséhez; a főképernyőre csak akkor íródik vissza az eredmény, ha
        # itt UGYANAZ az rr van beállítva, mint a mentett (fő ablak) rr.
        rrbar = tk.Frame(win, bg=BG)
        rrbar.pack(anchor="w", padx=12, pady=(2, 0))
        tk.Label(rrbar, text="Kockázatcsökkentés:", bg=BG, fg=FG_GRAY,
                 font=self._sf).pack(side="left")
        self._rr_name = tk.StringVar(value=_rrs.NAME.get(self._init_preset, "Ki"))
        om = tk.OptionMenu(rrbar, self._rr_name, *[_rrs.NAME[p] for p in _rrs.CYCLE])
        om.config(bg=BG_HEADER, fg=FG_WHITE, font=self._sf, relief="flat",
                  highlightthickness=0, activebackground=BG_HEADER)
        om["menu"].config(bg=BG_HEADER, fg=FG_WHITE)
        om.pack(side="left", padx=(4, 0))
        self._cautious_var = tk.BooleanVar(value=self._init_cautious)
        tk.Checkbutton(rrbar, text="Óvatos méret", variable=self._cautious_var,
                       bg=BG, fg=FG_GRAY, selectcolor=BG_HEADER, font=self._sf,
                       activebackground=BG, activeforeground=FG_WHITE).pack(
                       side="left", padx=(10, 0))
        tk.Label(rrbar, text="Runner:", bg=BG, fg=FG_GRAY,
                 font=self._sf).pack(side="left", padx=(10, 0))
        self._runner_name = tk.StringVar(
            value=_rrs.RUNNER_NAME.get(self._init_runner, "Trailing"))
        omr = tk.OptionMenu(rrbar, self._runner_name,
                            *[_rrs.RUNNER_NAME[r] for r in _rrs.RUNNERS])
        omr.config(bg=BG_HEADER, fg=FG_WHITE, font=self._sf, relief="flat",
                   highlightthickness=0, activebackground=BG_HEADER)
        omr["menu"].config(bg=BG_HEADER, fg=FG_WHITE)
        omr.pack(side="left", padx=(4, 0))
        tk.Label(win, text="(feltáró — nem ment; a főképernyőre csak az eredeti rr "
                           "eredménye íródik vissza)", bg=BG, fg=FG_GRAY_DIM,
                 font=self._sf).pack(anchor="w", padx=12, pady=(1, 4))

        # ── Progress ────────────────────────────────────────────────────────
        pf = tk.Frame(win, bg=BG)
        pf.pack(fill="x", padx=12, pady=(2, 2))
        self._pbar = ttk.Progressbar(pf, orient="horizontal", mode="determinate",
                                     maximum=100.0, length=380)
        self._pbar.pack(side="left")
        self._pct_lbl = tk.Label(pf, text="0%", bg=BG, fg=FG_GRAY, font=self._sf,
                                 width=6)
        self._pct_lbl.pack(side="left", padx=(8, 0))

        # ── Élő kijelzés ────────────────────────────────────────────────────
        live = tk.Frame(win, bg=BG)
        live.pack(anchor="w", padx=12, pady=(2, 2))
        self._live = {}
        for key, label in (("time", "Idő "), ("balance", "Egyenleg "),
                           ("open", "Nyitott "), ("closed", "Lezárt ")):
            cell = tk.Frame(live, bg=BG)
            cell.pack(side="left", padx=(0, 14))
            tk.Label(cell, text=label, bg=BG, fg=FG_GRAY,
                     font=self._sf).pack(side="left")
            v = tk.Label(cell, text="—", bg=BG, fg=FG_WHITE, font=self._sf)
            v.pack(side="left")
            self._live[key] = v
        self._tech_lbl = tk.Label(win, text="", bg=BG, fg=FG_GRAY_DIM,
                                  font=self._sf)
        self._tech_lbl.pack(anchor="w", padx=12, pady=(0, 2))

        # ── Egyenleg-görbe (sparkline) ──────────────────────────────────────
        self._canvas = tk.Canvas(win, width=400, height=90, bg=BG_HEADER,
                                 highlightthickness=0)
        self._canvas.pack(anchor="w", padx=12, pady=(4, 4))

        # ── Eredmény-sáv (minősítés + metrikák) ─────────────────────────────
        self._grade_lbl = tk.Label(win, bg=BG, font=self._hf, anchor="w",
                                   text="")
        self._grade_lbl.pack(anchor="w", padx=12, pady=(2, 0))
        self._metrics_frame = tk.Frame(win, bg=BG)
        self._metrics_frame.pack(anchor="w", padx=12, pady=(0, 4))

        self._status = tk.Label(win, text="", bg=BG, fg=FG_GRAY, font=self._sf)
        self._status.pack(anchor="w", padx=12)

        # ── Gombok ──────────────────────────────────────────────────────────
        btns = tk.Frame(win, bg=BG)
        btns.pack(pady=10)
        self._btn_start = tk.Button(btns, text="Backtest indítása", bg=BTN_BT_BG,
                                    fg=BTN_BT_FG, relief="flat", font=self._sf,
                                    state="disabled", command=self._start)
        self._btn_start.pack(side="left", padx=6)
        tk.Button(btns, text="Bezárás", bg=BTN_DIS_BG, fg=BTN_DIS_FG, relief="flat",
                  font=self._sf, command=self._close).pack(side="left", padx=6)

        # A szülő (Stratégia Paraméterek) ablak grab_set-tel modális → a gyereknek
        # is meg kell fognia a grabot, különben kattinthatatlan. Záráskor a grabot
        # visszaadjuk a szülőnek.
        win.grab_set()
        win.protocol("WM_DELETE_WINDOW", self._close)

    def _close(self):
        try:
            self.parent.grab_set()
        except Exception:
            pass
        self.win.destroy()

    # ── Adatbetöltés (háttér) ────────────────────────────────────────────────
    def _load_data_async(self):
        self._span_lbl.config(text="Adat betöltése…", fg=FG_GRAY_DIM)

        def work():
            df15 = df1 = None
            err = None
            try:
                from trading.backtest import load_data
                df15, df1 = load_data(self.symbol)
                if df15 is None:
                    err = "Nincs letöltött adat (data/m15, data/m1) ehhez a párhoz."
            except Exception as ex:
                err = str(ex)
            try:
                self.win.after(0, lambda: self._data_ready(df15, df1, err))
            except Exception:
                pass

        threading.Thread(target=work, daemon=True, name="BtDlgLoad").start()

    def _data_ready(self, df15, df1, err):
        if err:
            self._span_lbl.config(text=err, fg=FG_RED)
            return
        self._df15, self._df1 = df15, df1
        try:
            lo_ts, hi_ts = df1.index[0], df1.index[-1]
            lo = lo_ts.strftime("%Y-%m-%d")
            hi = hi_ts.strftime("%Y-%m-%d")
            # Alap-időszak: az utolsó ~18 hónap (a régebbi, eltérő piac alapból
            # kimarad — a teljes tartomány látszik és a mezőben bővíthető).
            default_start = max(lo_ts, hi_ts - pd.DateOffset(months=18))
            ds = default_start.strftime("%Y-%m-%d")
            self._span_lbl.config(
                text=f"Elérhető: {lo} … {hi}  (alap: az utolsó ~18 hónap)",
                fg=FG_GRAY_DIM)
            if not self._start_var.get():
                self._start_var.set(ds)
            if not self._end_var.get():
                self._end_var.set(hi)
        except Exception:
            self._span_lbl.config(text="Adat betöltve.", fg=FG_GRAY_DIM)
        self._btn_start.config(state="normal")

    # ── Futtatás ─────────────────────────────────────────────────────────────
    def _start(self):
        if self._running or self._df1 is None:
            return
        self._running = True
        self._btn_start.config(text="Fut…", state="disabled")
        self._status.config(text="Backtest fut…", fg=FG_GRAY)
        self._pbar.config(value=0.0)
        self._pct_lbl.config(text="0%")
        self._canvas.delete("all")
        for w in self._metrics_frame.winfo_children():
            w.destroy()
        self._grade_lbl.config(text="")

        start = self._start_var.get().strip() or None
        end   = self._end_var.get().strip() or None
        ib = float(self.cfg.get("ml", {}).get("starting_balance_eur", 1000.0))
        rr_spec = self._current_rr_spec()          # az ablakban választott (feltáró) rr

        def cb(pct, m1_time, balance, n_open, n_closed, tech):
            try:
                self.win.after(0, lambda: self._on_progress(
                    pct, m1_time, balance, n_open, n_closed, tech))
            except Exception:
                pass

        def work():
            summary, result, err = None, None, None
            try:
                from trading.backtest import run_pair
                result = run_pair(self.symbol, self._df15, self._df1, self.params,
                                  self.pair_cfg, self.cfg["trading"], ib,
                                  strategy=self.strategy, rr=rr_spec,
                                  test_start=start, test_end=end,
                                  progress_callback=cb)
                summary = result.summary(ib)
                from collections import Counter
                tech = Counter(t.rr_technique for t in result.closed
                               if getattr(t, "rr_technique", ""))
                if summary and tech:
                    summary["_rr_tech"] = dict(tech)
                # MT5 backtest-reprodukció: a belépők CSV-be (BacktestReplayer.mq5
                # replay). „Amikor futtatok egy backtestet, elkészíti a belépőket."
                try:
                    from tools.mt5_export import export_mt5_csv
                    from version import BASE_DIR
                    _p = export_mt5_csv(result, self.symbol, self.params,
                                        self.pair_cfg, BASE_DIR / "data" / "mt5_backtest")
                    if _p and summary is not None:
                        summary["_mt5_csv"] = _p.name
                except Exception:
                    pass
            except Exception as ex:
                err = str(ex)
            try:
                self.win.after(0, lambda: self._done(summary, result, ib, err))
            except Exception:
                pass

        threading.Thread(target=work, daemon=True, name="BtDlgRun").start()

    def _on_progress(self, pct, m1_time, balance, n_open, n_closed, tech):
        self._pbar.config(value=pct * 100.0)
        self._pct_lbl.config(text=f"{pct * 100:.0f}%")
        try:
            self._live["time"].config(text=str(m1_time)[:16])
        except Exception:
            pass
        col = FG_GREEN if balance >= 0 else FG_RED
        self._live["balance"].config(text=f"{balance:,.0f}$", fg=col)
        self._live["open"].config(text=str(n_open))
        self._live["closed"].config(text=str(n_closed))
        if tech:
            self._tech_lbl.config(text="Technika: " + ", ".join(
                f"{_TECH_NAMES.get(k, k)}×{v}" for k, v in tech.items()))

    def _done(self, summary, result, ib, err):
        self._running = False
        try:
            self._btn_start.config(text="Backtest indítása", state="normal")
        except Exception:
            return   # az ablak közben bezárult
        if err:
            self._status.config(text=f"Backtest hiba: {err}", fg=FG_RED)
            return
        self._pbar.config(value=100.0)
        self._pct_lbl.config(text="100%")
        tech = (summary or {}).pop("_rr_tech", None) or {}
        _tech_txt = ""
        if tech:
            _tech_txt = "Ténylegesen alkalmazott technika: " + ", ".join(
                f"{_TECH_NAMES.get(k, k)}×{v}" for k, v in tech.items())
        # MT5 backtest-reprodukció CSV neve (a metrikák közül kivéve).
        _mt5 = (summary or {}).pop("_mt5_csv", None)
        if _mt5:
            _tech_txt = (_tech_txt + "   |   " if _tech_txt else "") + \
                        f"MT5 CSV: data/mt5_backtest/{_mt5}"
        if _tech_txt:
            self._tech_lbl.config(text=_tech_txt)
        self._summary = summary
        self._render_metrics(summary)
        if result is not None:
            self._draw_equity(result, ib)
        # Visszaírás a főképernyőre CSAK ha ugyanazt az rr-t mértük, mint a mentett
        # (fő ablak) rr — különben ez feltáró futtatás, nem szennyezi a mentendőt.
        same_rr = self._rr_key(self._current_rr_spec()) == self._opened_rr_key
        if same_rr and self._on_result and summary:
            try:
                self._on_result(summary)
                self._status.config(text="Kész — az eredmény a főképernyőre írva.",
                                    fg=FG_GREEN)
            except Exception:
                self._status.config(text="Kész.", fg=FG_GREEN)
        else:
            self._status.config(
                text="Kész (feltáró rr — nem íródik vissza a főképernyőre).",
                fg=FG_YELLOW)

    # ── Renderelés ────────────────────────────────────────────────────────────
    def _render_metrics(self, summary):
        for w in self._metrics_frame.winfo_children():
            w.destroy()
        if not summary or summary.get("trades", 0) == 0:
            self._grade_lbl.config(text="Minősítés: —", fg=FG_GRAY)
            tk.Label(self._metrics_frame, text="0 trade ezen a paraméterezésen",
                     bg=BG, fg=FG_YELLOW, font=self._sf).pack(side="left")
            return
        gtxt, gcol, greason = self.strategy.grade(summary, self.cfg)
        self._grade_lbl.config(
            text=f"Minősítés: {gtxt}" + (f"   ({greason})" if greason else ""),
            fg=sem_color(gcol))
        mc = metric_colors(summary, self.cfg)
        for label, fn, key in _METRIC_ORDER:
            color = "white" if key is None else mc.get(key, "white")
            cell = tk.Frame(self._metrics_frame, bg=BG)
            cell.pack(side="left", padx=(0, 12))
            tk.Label(cell, text=label, bg=BG, fg=FG_GRAY,
                     font=self._sf).pack(side="left")
            tk.Label(cell, text=fn(summary), bg=BG, fg=sem_color(color),
                     font=self._sf).pack(side="left")

    def _draw_equity(self, result, ib):
        """Egyszerű egyenleg-görbe a balance_curve-ből (matplotlib nélkül)."""
        c = self._canvas
        c.delete("all")
        curve = getattr(result, "balance_curve", None) or []
        W = int(c.cget("width")); H = int(c.cget("height"))
        pad = 6
        if len(curve) < 2:
            c.create_text(W // 2, H // 2, text="nincs elég adat a görbéhez",
                          fill=FG_GRAY_DIM, font=self._sf)
            return
        ys = [ib] + [b for _, b in curve]
        lo, hi = min(ys), max(ys)
        rng = (hi - lo) or 1.0
        n = len(ys)

        def px(i):
            return pad + (W - 2 * pad) * i / (n - 1)

        def py(v):
            return H - pad - (H - 2 * pad) * (v - lo) / rng

        # Nulla-referencia (kezdő egyenleg) vonala
        if lo <= ib <= hi:
            y0 = py(ib)
            c.create_line(pad, y0, W - pad, y0, fill=FG_GRAY_DIM, dash=(2, 3))
        pts = []
        for i, v in enumerate(ys):
            pts += [px(i), py(v)]
        final = ys[-1]
        line_col = "#3fb950" if final >= ib else "#f85149"
        c.create_line(*pts, fill=line_col, width=2, smooth=False)
