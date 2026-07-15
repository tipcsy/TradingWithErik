"""
Backtest-ablak (B3) — a Stratégia Paraméterek ablak „Backtest" gombja nyitja.

Szabványos, önálló ablak egy paraméterkészlet backtesteléséhez:
  • a backtestelt PARAMÉTEREK láthatók és SZERKESZTHETŐK (feltáró) — „Vissza"
    visszaállítja a megnyitáskori értékeket, „Mentés a Paraméterekhez" visszaírja
    az aktuális készletet a szülő (Stratégia Paraméterek) űrlapjába,
  • állítható időszak (kezdő/záró dátum; üresen = a teljes letöltött history),
  • választható óra-kapu: „Csak a kereskedési órákban" (trade_hours, mint élesben) —
    ekkor a `no_trade_resets_signal` param is életbe lép (a szünet reseteli az M15-öt),
  • állítható Kockázatcsökkentés + Óvatos méret + Runner + Exit (indikátor+paraméterek)
    + Építés (Ki/Kézi/Auto + méret-faktor) — mind FELTÁRÓ (nem ment, nem érinti a live-ot),
  • progress bar + százalék a futás közben,
  • élő kijelzés: aktuális szimulált idő, egyenleg, nyitott/lezárt kötések,
    és a ténylegesen alkalmazott kockázati technikák (Felező/Pajzs/Risky) száma,
  • a végén: minősítés + metrikák (Trade·Win·MaxDD·P&L·PF) és egy egyszerű
    egyenleg-görbe (sparkline) — az ELŐZŐ / EREDETI futás halványan összevethető.

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
from core.params_store import resolve_trade_hours
from core import rr_state as _rrs
from core import risk_reduction as _rrx
from core import build_state as _bst

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

# Az exit-indikátor emberi nevei + indikátor-függő SZERKESZTHETŐ paraméter-mezők
# (kulcs, rövid címke) — az instrumentum-ablakkal EGYEZŐEN (egy igazságforrás elv).
_EXIND_NAME = {"supertrend": "Supertrend", "wpr": "WPR", "divergence": "Divergencia"}
_EXIT_PARAM_SPEC = {
    "supertrend": [("st_period", "Per"), ("st_multiplier", "Szorzó")],
    "wpr":        [("wpr_period", "Per"), ("wpr_ma_period", "MA")],
    "divergence": [("osc", "Oszc"), ("div_period", "Per"), ("div_pivot", "Pivot")],
}


def _num(s):
    """Magyar-tizedes ('1,75') vagy sima szám → float; hiba esetén None."""
    try:
        return float(str(s).replace(",", "."))
    except (ValueError, TypeError):
        return None


class BacktestDialog:
    """Önálló backtest-ablak egy adott paraméterkészlethez."""

    def __init__(self, parent, symbol, cfg, strategy, params, pair_cfg,
                 rr_spec, header_font, small_font, on_result=None,
                 preset_name: str = "Ki", on_apply_params=None):
        self.parent   = parent
        self.symbol   = symbol
        self.cfg      = cfg
        self.strategy = strategy
        self.params   = dict(params)
        # A megnyitáskori paraméterek — a „Vissza" gomb ide állít vissza; a típus-
        # minta (int/float/bool/str) a szerkesztett érték visszakonvertálásához.
        self._init_params = dict(params)
        self._param_keys  = sorted(k for k in params if not str(k).startswith("_"))
        self.pair_cfg = pair_cfg
        self.rr_spec  = rr_spec
        self._hf      = header_font
        self._sf      = small_font
        self._on_result = on_result
        self._on_apply_params = on_apply_params   # visszaírás a szülő űrlapba
        self._preset_name = preset_name

        # A megnyitáskori (fő ablak) rr — ehhez viszonyítunk a visszaíráskor:
        # csak akkor írunk vissza a főképernyőre, ha a Backtest-ablakban ugyanezt
        # az rr-t mértük (különben feltáró futtatás, nem szennyezi a mentendőt).
        self._opened_rr_key = self._rr_key(rr_spec)
        _s0 = rr_spec or {}
        self._init_preset   = _s0.get("preset", _rrx.PRESET_OFF)
        self._init_runner   = _s0.get("runner_stop", _rrx.RUNNER_TRAILING)
        self._init_cautious = bool(_s0.get("cautious", False))
        # Exit-config (FELTÁRÓ, LOKÁLIS — nem írjuk a per-pár állapotba). A megnyitáskori
        # rr-spec exitjéből indul, különben a per-pár mentett exit-configból.
        _ex0 = dict((_s0.get("exit") or _rrs.get_exit_config(symbol)))
        self._exit_cfg = _ex0
        # Építés (FELTÁRÓ, LOKÁLIS) — a per-pár mentett módból/faktorból indul.
        self._init_build = _bst.get_config(symbol)

        # Előző / eredeti futás (a #4 összevetéshez)
        self._cur_result  = None
        self._cur_summary = None
        self._prev_result = None
        self._prev_summary = None
        self._orig_result = None
        self._orig_summary = None
        self._ib = float(cfg.get("ml", {}).get("starting_balance_eur", 1000.0))

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
        """Az ablakban BEÁLLÍTOTT rr-spec (feltáró). None, ha 'Ki'.
        Tartalmazza az Exit-configot is (a Runner=Kiszállási jel dönti, aktív-e)."""
        preset = self._preset_from_name(self._rr_name.get())
        if preset == _rrx.PRESET_OFF:
            return None
        runner = self._runner_from_name(self._runner_name.get())
        exit_cfg = dict(self._exit_cfg)
        exit_cfg["enabled"] = (runner == _rrx.RUNNER_EXIT)
        return {**_rrx.default_config(), "preset": preset,
                "runner_stop": runner,
                "cautious": bool(self._cautious_var.get()),
                "exit": exit_cfg}

    def _allowed_hours(self):
        """A backtest óra-kapuja. None → minden óra (a checkbox KI). Bekapcsolva a
        stratégia kereskedési órái (trade_hours), a live `process_pair`-rel EGYEZŐ
        feloldással: stratégia-hatókörű `{symbol}_hours.json` → legacy trade_hours →
        sess_start/sess_end tartomány."""
        if not self._hours_filter_var.get():
            return None
        th = resolve_trade_hours(self.symbol, self.strategy.name,
                                 self.pair_cfg.get("trade_hours"))
        if th is not None:
            return {int(h) for h in th}
        return set(range(int(self.pair_cfg.get("sess_start", 0)),
                         int(self.pair_cfg.get("sess_end", 24))))

    def _current_build_cfg(self):
        """Az ablakban BEÁLLÍTOTT építés-config (feltáró) — {mode, size_factor}."""
        mode = {v: k for k, v in _bst.NAME.items()}.get(
            self._build_mode_name.get(), _bst.MODE_OFF)
        sf = _num(self._build_sf_var.get())
        return {"mode": mode, "size_factor": sf if sf and sf > 0 else 0.7}

    # ── UI ──────────────────────────────────────────────────────────────────
    def _build(self):
        win = tk.Toplevel(self.parent)
        self.win = win
        win.title(f"{self.symbol} — {self.strategy.name} Backtest")
        win.configure(bg=BG)

        tk.Label(win, text=f"{self.symbol}  ·  {self.strategy.name} — Backtest",
                 bg=BG, fg=FG_WHITE, font=self._hf).pack(anchor="w", padx=12, pady=(12, 2))

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

        # ── Óra-kapu (kereskedési órák szűrése) ─────────────────────────────
        # Ha bekapcsolod, a backtest CSAK a stratégia kereskedési óráiban (trade_hours,
        # mint a live) nyit — a többi óra kimarad, és ha a `no_trade_resets_signal`
        # param be van kapcsolva, a szünet reseteli az M15 ablakot (mint élesben).
        # Alap: KI → minden órában kereskedik (a korábbi backtest-ablak viselkedése).
        hrow = tk.Frame(win, bg=BG)
        hrow.pack(anchor="w", padx=12, pady=(0, 4))
        self._hours_filter_var = tk.BooleanVar(value=False)
        tk.Checkbutton(hrow, text="Csak a kereskedési órákban (trade_hours, mint élesben)",
                       variable=self._hours_filter_var, bg=BG, fg=FG_GRAY,
                       selectcolor=BG_HEADER, font=self._sf, activebackground=BG,
                       activeforeground=FG_WHITE).pack(side="left")

        # ── Paraméterek (SZERKESZTHETŐ — feltáró) ───────────────────────────
        phdr = tk.Frame(win, bg=BG)
        phdr.pack(fill="x", padx=12, pady=(2, 0))
        tk.Label(phdr, text="Paraméterek (szerkeszthető — feltáró):", bg=BG,
                 fg=FG_GRAY, font=self._sf).pack(side="left")
        tk.Button(phdr, text="Vissza", bg=BG_HEADER, fg=FG_WHITE, relief="flat",
                  font=self._sf, cursor="hand2", command=self._reset_params).pack(
                  side="left", padx=(8, 0))
        pform = tk.Frame(win, bg=BG)
        pform.pack(anchor="w", padx=12, pady=(2, 2))
        self._pentries = {}
        _COLS = 2
        for i, k in enumerate(self._param_keys):
            r, c = divmod(i, _COLS)
            cell = tk.Frame(pform, bg=BG)
            cell.grid(row=r, column=c, sticky="w", padx=(0, 12), pady=1)
            tk.Label(cell, text=k, bg=BG, fg=FG_WHITE, font=self._sf,
                     anchor="w", width=22).pack(side="left")
            e = tk.Entry(cell, width=9, bg=BG_HEADER, fg=FG_WHITE, font=self._sf,
                         insertbackground=FG_WHITE)
            e.insert(0, str(self._init_params[k]))
            e.pack(side="left")
            self._pentries[k] = e

        # ── Kockázatcsökkentés + runner + exit + építés (FELTÁRÓ) ───────────
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

        # ── Exit-indikátor + paraméterei (feltáró) + Építés ─────────────────
        exbar = tk.Frame(win, bg=BG)
        exbar.pack(anchor="w", padx=12, pady=(2, 0))
        tk.Label(exbar, text="Exit:", bg=BG, fg=FG_GRAY, font=self._sf).pack(side="left")
        _exind = self._exit_cfg.get("indicator", "supertrend")
        self._exit_ind_name = tk.StringVar(value=_EXIND_NAME.get(_exind, "Supertrend"))
        ome = tk.OptionMenu(exbar, self._exit_ind_name, *_EXIND_NAME.values(),
                            command=self._on_exit_ind_change)
        ome.config(bg=BG_HEADER, fg=FG_WHITE, font=self._sf, relief="flat",
                   highlightthickness=0, activebackground=BG_HEADER)
        ome["menu"].config(bg=BG_HEADER, fg=FG_WHITE)
        ome.pack(side="left", padx=(4, 0))
        self._exit_pfrm = tk.Frame(exbar, bg=BG)
        self._exit_pfrm.pack(side="left", padx=(6, 0))
        self._exit_param_vars = {}
        self._rebuild_exit_params()

        tk.Label(exbar, text="Építés:", bg=BG, fg=FG_GRAY, font=self._sf).pack(side="left", padx=(10, 0))
        self._build_mode_name = tk.StringVar(
            value=_bst.NAME.get(self._init_build.get("mode", _bst.MODE_OFF), "Ki"))
        omb = tk.OptionMenu(exbar, self._build_mode_name, *_bst.NAME.values())
        omb.config(bg=BG_HEADER, fg=FG_WHITE, font=self._sf, relief="flat",
                   highlightthickness=0, activebackground=BG_HEADER)
        omb["menu"].config(bg=BG_HEADER, fg=FG_WHITE)
        omb.pack(side="left", padx=(4, 0))
        tk.Label(exbar, text="Faktor:", bg=BG, fg=FG_GRAY, font=self._sf).pack(side="left", padx=(6, 0))
        self._build_sf_var = tk.StringVar(value=str(self._init_build.get("size_factor", 0.7)))
        tk.Entry(exbar, textvariable=self._build_sf_var, width=5, bg=BG_HEADER,
                 fg=FG_WHITE, font=self._sf, relief="flat",
                 insertbackground=FG_WHITE).pack(side="left", padx=(2, 0))

        tk.Label(win, text="(feltáró — nem ment; az Építés csak Auto+Ki-preset esetén "
                           "modelleződik. A főképernyőre csak az eredeti rr eredménye "
                           "íródik vissza.)", bg=BG, fg=FG_GRAY_DIM,
                 font=self._sf, justify="left", wraplength=620).pack(
                 anchor="w", padx=12, pady=(1, 4))

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

        # ── Összevetés-választó (előző/eredeti futás halvány overlay) ───────
        cmp_bar = tk.Frame(win, bg=BG)
        cmp_bar.pack(anchor="w", padx=12, pady=(2, 0))
        tk.Label(cmp_bar, text="Összevetés:", bg=BG, fg=FG_GRAY,
                 font=self._sf).pack(side="left")
        self._overlay_mode = tk.StringVar(value="Előző")
        omc = tk.OptionMenu(cmp_bar, self._overlay_mode, "Nincs", "Előző", "Eredeti",
                            command=lambda _=None: self._on_overlay_change())
        omc.config(bg=BG_HEADER, fg=FG_WHITE, font=self._sf, relief="flat",
                   highlightthickness=0, activebackground=BG_HEADER)
        omc["menu"].config(bg=BG_HEADER, fg=FG_WHITE)
        omc.pack(side="left", padx=(4, 0))
        self._ref_metrics_lbl = tk.Label(cmp_bar, text="", bg=BG, fg=FG_GRAY_DIM,
                                         font=self._sf)
        self._ref_metrics_lbl.pack(side="left", padx=(10, 0))

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
        self._btn_apply = tk.Button(btns, text="Mentés a Paraméterekhez",
                                    bg=BTN_PLAY_BG, fg=BTN_PLAY_FG, relief="flat",
                                    font=self._sf, command=self._apply_params)
        if self._on_apply_params is None:
            self._btn_apply.config(state="disabled")
        self._btn_apply.pack(side="left", padx=6)
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

    # ── Exit-indikátor paraméterei (feltáró, lokális) ─────────────────────────
    def _on_exit_ind_change(self, name: str):
        ind = {v: k for k, v in _EXIND_NAME.items()}.get(name, "supertrend")
        self._exit_cfg["indicator"] = ind
        self._rebuild_exit_params()

    def _rebuild_exit_params(self):
        """Az exit-indikátor SZERKESZTHETŐ mezőinek újraépítése (a kiválasztott
        indikátor szerint), a LOKÁLIS exit-configból feltöltve."""
        for w in self._exit_pfrm.winfo_children():
            w.destroy()
        self._exit_param_vars = {}
        ind = {v: k for k, v in _EXIND_NAME.items()}.get(
            self._exit_ind_name.get(), "supertrend")
        for key, label in _EXIT_PARAM_SPEC.get(ind, []):
            tk.Label(self._exit_pfrm, text=f"{label}:", bg=BG, fg=FG_GRAY,
                     font=self._sf).pack(side="left")
            var = tk.StringVar(value=str(self._exit_cfg.get(key, "")))
            e = tk.Entry(self._exit_pfrm, textvariable=var,
                         width=(5 if key == "osc" else 4), bg=BG_HEADER,
                         fg=FG_WHITE, font=self._sf, relief="flat",
                         insertbackground=FG_WHITE)
            e.pack(side="left", padx=(2, 6))
            e.bind("<FocusOut>", lambda ev, k=key: self._save_exit_param(k))
            e.bind("<Return>",   lambda ev, k=key: self._save_exit_param(k))
            self._exit_param_vars[key] = var

    def _save_exit_param(self, key: str):
        """Egy exit-paraméter a LOKÁLIS configba (típus-validálással)."""
        raw = self._exit_param_vars[key].get().strip()
        if key == "osc":
            val = raw.lower() if raw.lower() in ("rsi", "cci") else "rsi"
        elif key == "st_multiplier":
            try:
                val = float(raw)
            except ValueError:
                return
        else:
            try:
                val = int(float(raw))
            except ValueError:
                return
        self._exit_cfg[key] = val

    # ── Paraméter-szerkesztés (feltáró) ──────────────────────────────────────
    def _reset_params(self):
        """„Vissza" — a megnyitáskori paraméterek visszaállítása az űrlapon."""
        for k, e in self._pentries.items():
            e.delete(0, "end")
            e.insert(0, str(self._init_params[k]))
        self._status.config(text="Paraméterek visszaállítva a megnyitáskori értékekre.",
                            fg=FG_GRAY)

    def _collect_params(self):
        """Az Entry-k tartalma → típusos paraméter-dict (a megnyitáskori típus
        szerint). A nem szerkeszthető (_ kezdetű) kulcsokat átvisszük. Hiba → None."""
        new = {k: v for k, v in self.params.items() if str(k).startswith("_")}
        for k in self._param_keys:
            raw = self._pentries[k].get().strip()
            orig = self._init_params.get(k)
            try:
                if isinstance(orig, bool):
                    new[k] = raw.lower() in ("true", "1", "igen", "yes")
                elif isinstance(orig, int):
                    new[k] = int(float(raw))
                elif isinstance(orig, float):
                    new[k] = float(raw)
                else:
                    fv = _num(raw)
                    new[k] = fv if (fv is not None and raw != "") else raw
            except ValueError:
                self._status.config(text=f"Hibás érték: {k} = {raw!r}", fg=FG_RED)
                return None
        return new

    def _apply_params(self):
        """„Mentés a Paraméterekhez" — az aktuális készletet visszaírja a szülő
        (Stratégia Paraméterek) űrlapjába (nem perzisztál lemezre; azt a szülő
        Mentés gombja teszi). Ha volt friss backtest, az eredményt is átadja."""
        if self._on_apply_params is None:
            return
        params = self._collect_params()
        if params is None:
            return
        try:
            self._on_apply_params(params, self._cur_summary)
            self._status.config(
                text="A paraméterek visszaírva a Stratégia Paraméterek űrlapjába "
                     "(a Mentés gomb perzisztálja).", fg=FG_GREEN)
        except Exception as ex:
            self._status.config(text=f"Visszaírási hiba: {ex}", fg=FG_RED)

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
        params = self._collect_params()
        if params is None:
            return
        self._run_params = params
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
        ib = self._ib
        rr_spec = self._current_rr_spec()          # az ablakban választott (feltáró) rr
        build_cfg = self._current_build_cfg()      # az ablakban választott (feltáró) építés
        allowed = self._allowed_hours()            # None = minden óra; különben trade_hours

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
                result = run_pair(self.symbol, self._df15, self._df1, params,
                                  self.pair_cfg, self.cfg["trading"], ib,
                                  strategy=self.strategy, rr=rr_spec,
                                  build=build_cfg, allowed_hours=allowed,
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
                    _p = export_mt5_csv(result, self.symbol, params,
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

        # Előző/eredeti futás görgetése (a #4 összevetéshez): a most lecserélt
        # aktuális lesz az „előző"; az első valaha futott az „eredeti".
        self._prev_result, self._prev_summary = self._cur_result, self._cur_summary
        self._cur_result,  self._cur_summary  = result, summary
        if self._orig_result is None and result is not None:
            self._orig_result, self._orig_summary = result, summary

        self._render_metrics(summary)
        self._redraw()
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

    # ── Összevetés (előző/eredeti) ────────────────────────────────────────────
    def _reference(self):
        """A kiválasztott összevetési (referencia) futás (result, summary, címke).
        (None, None, "") ha nincs / „Nincs" van választva."""
        mode = self._overlay_mode.get()
        if mode == "Előző":
            return self._prev_result, self._prev_summary, "Előző"
        if mode == "Eredeti":
            return self._orig_result, self._orig_summary, "Eredeti"
        return None, None, ""

    def _on_overlay_change(self):
        self._redraw()

    def _fmt_ref_metrics(self, summary, label):
        if not summary or summary.get("trades", 0) == 0:
            return ""
        pf = summary.get("profit_factor", 0)
        pf_s = "∞" if pf == float("inf") else f"{pf:.2f}"
        return (f"{label}: Trade {int(summary.get('trades', 0))} · "
                f"Win {summary.get('win_rate', 0) * 100:.0f}% · "
                f"P&L {summary.get('total_pnl', 0):+.0f}$ · PF {pf_s}")

    def _redraw(self):
        """Az egyenleg-görbe újrarajzolása: az aktuális futás + (opcionálisan) a
        kiválasztott referencia (előző/eredeti) HALVÁNYAN, közös skálán."""
        ref_result, ref_summary, ref_label = self._reference()
        self._ref_metrics_lbl.config(
            text=self._fmt_ref_metrics(ref_summary, ref_label))
        self._draw_equity(self._cur_result, self._ib, ref_result)

    def _draw_equity(self, result, ib, ref_result=None):
        """Egyenleg-görbe a balance_curve-ből (matplotlib nélkül). A `ref_result`
        (ha van) HALVÁNYAN, ugyanazon a skálán rajzolódik az összevetéshez."""
        c = self._canvas
        c.delete("all")
        W = int(c.cget("width")); H = int(c.cget("height"))
        pad = 6

        def curve_ys(res):
            cur = getattr(res, "balance_curve", None) or [] if res is not None else []
            return [ib] + [b for _, b in cur] if len(cur) >= 1 else []

        ys_cur = curve_ys(result)
        ys_ref = curve_ys(ref_result)
        if len(ys_cur) < 2 and len(ys_ref) < 2:
            c.create_text(W // 2, H // 2, text="nincs elég adat a görbéhez",
                          fill=FG_GRAY_DIM, font=self._sf)
            return

        # Közös skála (mindkét görbét ugyanabba a tartományba rajzoljuk).
        allv = [v for v in (ys_cur + ys_ref)] or [ib]
        lo, hi = min(allv), max(allv)
        rng = (hi - lo) or 1.0

        def py(v):
            return H - pad - (H - 2 * pad) * (v - lo) / rng

        def draw(ys, color, width, dash=None):
            if len(ys) < 2:
                return
            n = len(ys)
            pts = []
            for i, v in enumerate(ys):
                x = pad + (W - 2 * pad) * i / (n - 1)
                pts += [x, py(v)]
            if dash:
                c.create_line(*pts, fill=color, width=width, smooth=False, dash=dash)
            else:
                c.create_line(*pts, fill=color, width=width, smooth=False)

        # Nulla-referencia (kezdő egyenleg) vonala
        if lo <= ib <= hi:
            y0 = py(ib)
            c.create_line(pad, y0, W - pad, y0, fill=FG_GRAY_DIM, dash=(2, 3))
        # Referencia (előző/eredeti) HALVÁNYAN, szaggatva — alulra.
        draw(ys_ref, FG_GRAY_DIM, 1, dash=(3, 3))
        # Aktuális futás — élénken, felülre.
        if len(ys_cur) >= 2:
            final = ys_cur[-1]
            line_col = "#3fb950" if final >= ib else "#f85149"
            draw(ys_cur, line_col, 2)
