"""
Instrumentum-paraméter szerkesztő ablak (a Symbol-cellára kattintva nyílik).

Korábban a `dashboard/gui.py`-ban élt (`_show_instrument_params`); kiszervezve,
mert a gui.py túl nagy lett és ez az ablak önállóan is bővül.

Mit tud:
  • Optimalizált párnál: minősítés + metrikák fejléc, a trials CSV **sorszám
    (minőségi rangsor, 1 = legjobb)** szerinti betöltése ▲/▼ nyilakkal, óránkénti
    kereskedési kapcsoló (trade_hours), kézi paraméter-módosítás.
  • Optimalizálatlan párnál (nincs JSON): ugyanez az ablak nyílik **alap-
    paraméterekkel**, így GUI-ból is létrehozható a `{symbol}.json` optimalizálás
    nélkül (rövid életű / friss instrumentumokhoz).
  • Kézi paraméter-készlet, ami nincs a listában, **új sorszámként** (501…)
    menthető a trials CSV-be és a JSON-ba.

A trials CSV formátuma: ';' elválasztó + ',' tizedes (magyar Excel), utf-8-sig.
Score szerint csökkenő sorrendben van → a sor sorszáma = minőségi rangsor.
"""

from __future__ import annotations

import csv
import json
import threading
import tkinter as tk
from datetime import datetime

from dashboard.theme import (
    BG, BG_HEADER,
    FG_WHITE, FG_GREEN, FG_RED, FG_YELLOW, FG_GRAY, FG_GRAY_DIM,
    BTN_PLAY_BG, BTN_PLAY_FG, BTN_BT_BG, BTN_BT_FG,
    BTN_DIS_BG, BTN_DIS_FG,
    color as sem_color,
)
from core.quality import metric_colors
from core.params_store import (
    params_file, trials_file, resolve_trade_hours, save_trade_hours,
)

# A trials CSV metrika-oszlopai (ezek NEM paraméterek, hanem az eredmény jellemzői)
_METRIC_COLS = frozenset({
    "rank", "score", "trades", "win_rate", "total_pnl", "max_drawdown",
    "profit_factor", "note",
})
# Az első manuálisan mentett sor sorszáma (megkülönbözteti az optimalizáltaktól)
_MANUAL_RANK_BASE = 501


def _num(s):
    """Magyar-tizedes ('1,75') vagy sima szám → float; hiba esetén None."""
    try:
        return float(str(s).replace(",", "."))
    except (ValueError, TypeError):
        return None


def default_params(cfg: dict, strategy) -> dict:
    """Alapértelmezett paraméter-készlet optimalizálatlan instrumentumhoz.

    A stratégia `base_params`-ából indul, és kiegészíti az optimizer-tér összes
    hangolható kulcsával (érték: a trading-config, különben a tartomány alja),
    hogy a kézi űrlap ugyanazt a teljes paraméterlistát kínálja, amit egy
    optimalizált JSON tartalmazna.
    """
    base = dict(strategy.base_params(cfg))
    opt = cfg.get("optimizer", {}) or {}
    trading = cfg.get("trading", {}) or {}
    for key, spec in opt.items():
        if key.startswith("_") or key in base:
            continue
        if not isinstance(spec, dict) or "min" not in spec:
            continue
        base[key] = trading.get(key, spec["min"])
    return base


class InstrumentParamsDialog:
    """Optimalizált paraméterek szerkesztője egy instrumentumhoz."""

    def __init__(self, parent, symbol, cfg, strategy,
                 header_font, small_font, save_main_config):
        self.parent  = parent
        self.symbol  = symbol
        self.cfg     = cfg
        self.strategy = strategy
        self._hf     = header_font
        self._sf     = small_font
        self._save_main_config = save_main_config

        # Stratégia-hatókörű tárolás: data/optimized_params/<strategy>/<symbol>.*
        self.pf = params_file(symbol, self.strategy.name)
        self.trials_csv = trials_file(symbol, self.strategy.name)

        # ── JSON betöltése (ha van) ─────────────────────────────────────────
        self.data = None
        try:
            if self.pf.exists():
                with open(self.pf, encoding="utf-8") as f:
                    self.data = json.load(f)
        except Exception:
            self.data = None
        self.is_new = self.data is None
        params = (self.data or {}).get("params", {})

        # A megjelenített/menthető paraméter-forrás: optimalizált JSON, vagy alap.
        # A (esetleg RÉGI sémájú) JSON-t a JELENLEGI sémához igazítjuk: a hiányzó ÚJ
        # kulcsokat kiegészítjük (alapérték), a meglévőket megtartjuk — így az új
        # paraméterek akkor is megjelennek/menthetők, ha a pár még nincs újraoptimali-
        # zálva. Migráció: a régi közös wpr_m15_trigger értékét átvisszük a külön
        # BUY/SELL triggerbe (a stratégiánál külön paraméter lett).
        if params:
            src = dict(params)
            if "wpr_m15_trigger" in src:
                _old = src.pop("wpr_m15_trigger")
                src.setdefault("wpr_m15_sell_trigger", _old)
                src.setdefault("wpr_m15_buy_trigger",  _old)
            for _k, _v in default_params(cfg, strategy).items():
                src.setdefault(_k, _v)
            self._src = src
        else:
            self._src = default_params(cfg, strategy)
        self._keys  = sorted(k for k in self._src if not k.startswith("_"))
        # Típus-minta a mentéskori konverzióhoz (int/float/bool/str)
        self._types = {k: self._src[k] for k in self._keys}

        # ── trials CSV betöltése → {rank: {oszlop: nyers_str}} ──────────────
        self._rank_rows = self._load_trials()
        self._ranks = sorted(self._rank_rows)

        # A Backtest gomb eredménye (a Mentés ezt írja test_summary-ként a JSON-ba,
        # így a minősítés megjelenik a soron). None = még nem futott backtest.
        self._bt_summary = None
        self._bt_running = False
        # Az egyetlen Mentés gomb új kombónál auto-backtestet futtat; ez a flag
        # jelzi a _bt_done-nak, hogy a backtest UTÁN folytassa a mentést.
        self._save_after_bt = False

        self._build()

    # ── trials CSV ──────────────────────────────────────────────────────────
    def _load_trials(self) -> dict:
        """A trials CSV beolvasása. Kulcs = sorszám (rank), érték = oszlop→str.

        A CSV score szerint csökkenő sorrendben van; ha nincs explicit `rank`
        oszlop, a sor pozíciója adja a rangsort (1 = első/legjobb)."""
        if not self.trials_csv.exists():
            return {}
        try:
            with open(self.trials_csv, encoding="utf-8-sig", newline="") as f:
                rows = list(csv.reader(f, delimiter=";"))
        except Exception:
            return {}
        if len(rows) < 2:
            return {}
        header = rows[0]
        out = {}
        for i, raw in enumerate((r for r in rows[1:] if r), start=1):
            rec = {header[j]: raw[j] for j in range(min(len(header), len(raw)))}
            if "rank" in rec:
                r = _num(rec["rank"])
                rank = int(r) if r is not None else i
            else:
                rank = i
            out[rank] = rec
        return out

    def _fmt_param(self, key: str, raw: str) -> str:
        """CSV-nyers érték → az Entry-be írható, tiszta szöveg (típus szerint)."""
        val = _num(raw)
        if val is None:
            return str(raw)
        t = self._types.get(key)
        if isinstance(t, bool):
            return "True" if val != 0 else "False"
        if isinstance(t, int):
            return str(int(round(val)))
        return f"{val:g}"          # 0.6000000000000001 → 0.6 ; 1.75 → 1.75

    # ── UI felépítés ─────────────────────────────────────────────────────────
    def _build(self):
        popup = tk.Toplevel(self.parent)
        self.popup = popup
        # A címben az instrumentum ÉS a stratégia is látszik (több stratégia esetén
        # egyértelmű, MELYIK stratégia paraméterei jelennek meg).
        title = f"{self.symbol} — {self.strategy.name} paraméterek"
        if self.is_new:
            title += " (új / kézi)"
        popup.title(title)
        popup.configure(bg=BG)
        popup.grab_set()

        # Fejléc-sor a tartalomban is (a címsor könnyen elsiklik): instrumentum + stratégia.
        tk.Label(popup, text=f"{self.symbol}  ·  stratégia: {self.strategy.name}",
                 bg=BG, fg=FG_WHITE, font=self._hf, anchor="w").pack(
                 anchor="w", padx=10, pady=(8, 0))

        ts = (self.data or {}).get("test_summary", {})

        # ── EGYETLEN metrika-sáv ────────────────────────────────────────────
        # Korábban UGYANAZ a metrika 3 helyen jelent meg (fejléc + sorszám-sor +
        # backtest-sor), más-más sorrendben. Most EGY sáv, ami a PILLANATNYILAG
        # betöltött paraméterkészletet tükrözi (mentett eredmény / #N trials-sor /
        # friss backtest), egységes sorrendben: Trade · Win · MaxDD · P&L · PF.
        self._grade_lbl = tk.Label(popup, bg=BG, font=self._hf, anchor="w")
        self._grade_lbl.pack(anchor="w", padx=10, pady=(10, 0))
        self._metrics_frame = tk.Frame(popup, bg=BG)
        self._metrics_frame.pack(anchor="w", padx=10, pady=(0, 1))
        self._src_lbl = tk.Label(popup, bg=BG, fg=FG_GRAY_DIM, font=self._sf,
                                 anchor="w")
        self._src_lbl.pack(anchor="w", padx=10, pady=(0, 4))
        if ts:
            self._render_metrics(ts, "mentett eredmény")
        else:
            self._render_metrics(
                None, "nincs mentett eredmény — állíts be paramétert, a Mentés "
                      "lefuttatja a backtestet és eltárolja")

        # ── Óra-rács (trade_hours) — a config.json-ba ment ──────────────────
        self._build_hours(popup, ts)

        # ── Kézi paraméter-űrlap ────────────────────────────────────────────
        tk.Label(popup, text="Kézi módosítás — a következő Play-nél lép életbe:",
                 bg=BG, fg=FG_GRAY, font=self._sf).pack(anchor="w", padx=10)

        # ── Sorszám-választó (csak ha van trials CSV) ───────────────────────
        self.lbl_rank = None
        if self._ranks:
            self._build_rank_selector(popup)

        form = tk.Frame(popup, bg=BG)
        form.pack(fill="both", expand=True, padx=10, pady=6)
        self.entries = {}
        for i, k in enumerate(self._keys):
            tk.Label(form, text=k, bg=BG, fg=FG_WHITE, font=self._sf,
                     anchor="w", width=24).grid(row=i, column=0, sticky="w", pady=1)
            e = tk.Entry(form, width=14, bg=BG_HEADER, fg=FG_WHITE,
                         font=self._sf, insertbackground=FG_WHITE)
            e.insert(0, str(self._src[k]))
            e.grid(row=i, column=1, padx=6, pady=1)
            # Kézi átírásnál a korábbi backtest-eredmény már nem érvényes → a Mentés
            # (auto-backtest) újraszámol. A programozott betöltés (rank) nem KeyRelease.
            e.bind("<KeyRelease>", lambda ev: self._invalidate_bt())
            self.entries[k] = e

        self.lbl_err = tk.Label(popup, text="", bg=BG, fg=FG_RED, font=self._sf)
        self.lbl_err.pack(anchor="w", padx=10)

        # ── Kockázatcsökkentés preset (per-pár) ─────────────────────────────
        # A Backtest gomb EZT méri; a Live-on a Fázis 3-mal lép majd életbe.
        from core import rr_state as _rrs
        self._rrs = _rrs
        rrbar = tk.Frame(popup, bg=BG)
        rrbar.pack(anchor="w", padx=10, pady=(4, 0))
        tk.Label(rrbar, text="Kockázatcsökkentés:", bg=BG, fg=FG_GRAY,
                 font=self._sf).pack(side="left")
        self._rr_name = tk.StringVar(value=_rrs.NAME.get(_rrs.get_preset(self.symbol), "Ki"))
        om = tk.OptionMenu(rrbar, self._rr_name, *[_rrs.NAME[p] for p in _rrs.CYCLE],
                           command=self._on_rr_change)
        om.config(bg=BG_HEADER, fg=FG_WHITE, font=self._sf, relief="flat",
                  highlightthickness=0, activebackground=BG_HEADER)
        om["menu"].config(bg=BG_HEADER, fg=FG_WHITE)
        om.pack(side="left", padx=(4, 0))

        # Haladó: Óvatos (felezett) méret pipa
        _c0 = _rrs.get_cautious(self.symbol)
        from core import risk_reduction as _rrx
        if _c0 is None:
            _c0 = _rrx.wants_cautious_size(_rrs.get_preset(self.symbol))
        self._cautious_var = tk.BooleanVar(value=bool(_c0))
        tk.Checkbutton(rrbar, text="Óvatos méret", variable=self._cautious_var,
                       bg=BG, fg=FG_GRAY, selectcolor=BG_HEADER, font=self._sf,
                       activebackground=BG, activeforeground=FG_WHITE,
                       command=self._on_cautious_change).pack(side="left", padx=(10, 0))

        # Haladó: runner stop
        tk.Label(rrbar, text="Runner:", bg=BG, fg=FG_GRAY,
                 font=self._sf).pack(side="left", padx=(10, 0))
        self._runner_name = tk.StringVar(
            value=_rrs.RUNNER_NAME.get(_rrs.get_runner(self.symbol), "Trailing"))
        omr = tk.OptionMenu(rrbar, self._runner_name,
                            *[_rrs.RUNNER_NAME[r] for r in _rrs.RUNNERS],
                            command=self._on_runner_change)
        omr.config(bg=BG_HEADER, fg=FG_WHITE, font=self._sf, relief="flat",
                   highlightthickness=0, activebackground=BG_HEADER)
        omr["menu"].config(bg=BG_HEADER, fg=FG_WHITE)
        omr.pack(side="left", padx=(4, 0))

        # Kiszállási jel indikátora — CSAK a „Kiszállási jel" runner-módnál él (a
        # Pajzs/Felező maradékát ez zárja, ha nincs konkrét TP). Supertrend (10/1.7)
        # vagy WPR-átzárás (20/100). A választás a per-pár exit-configba megy.
        self._EXIND_NAME = {"supertrend": "Supertrend", "wpr": "WPR",
                            "divergence": "Divergencia"}
        # Az indikátortól függő, SZERKESZTHETŐ paraméter-mezők (kulcs, rövid címke).
        self._EXIT_PARAM_SPEC = {
            "supertrend": [("st_period", "Per"), ("st_multiplier", "Szorzó")],
            "wpr":        [("wpr_period", "Per"), ("wpr_ma_period", "MA")],
            "divergence": [("osc", "Oszc"), ("div_period", "Per"), ("div_pivot", "Pivot")],
        }
        _exind = _rrs.get_exit_config(self.symbol).get("indicator", "supertrend")
        tk.Label(rrbar, text="Exit:", bg=BG, fg=FG_GRAY, font=self._sf).pack(side="left", padx=(10, 0))
        self._exit_ind_name = tk.StringVar(value=self._EXIND_NAME.get(_exind, "Supertrend"))
        ome = tk.OptionMenu(rrbar, self._exit_ind_name,
                            *self._EXIND_NAME.values(), command=self._on_exit_ind_change)
        ome.config(bg=BG_HEADER, fg=FG_WHITE, font=self._sf, relief="flat",
                   highlightthickness=0, activebackground=BG_HEADER)
        ome["menu"].config(bg=BG_HEADER, fg=FG_WHITE)
        ome.pack(side="left", padx=(4, 0))
        # Az indikátor paraméterei (az indikátor-váltáskor újraépül) — per-pár mentve.
        self._exit_pfrm = tk.Frame(rrbar, bg=BG)
        self._exit_pfrm.pack(side="left", padx=(6, 0))
        self._exit_param_vars = {}
        self._rebuild_exit_params()

        # Pozícióépítés mód (Ki/Kézi/Auto) — per instrumentum (a nyertes pozícióhoz
        # azonos irányú, csökkenő méretű ráépítések; a „＋" gomb a Pozíciók fülön).
        from core import build_state as _bst
        self._bst = _bst
        tk.Label(rrbar, text="Építés:", bg=BG, fg=FG_GRAY, font=self._sf).pack(side="left", padx=(10, 0))
        self._build_mode_name = tk.StringVar(value=_bst.NAME.get(_bst.get_mode(self.symbol), "Ki"))
        omb = tk.OptionMenu(rrbar, self._build_mode_name,
                            *_bst.NAME.values(), command=self._on_build_mode_change)
        omb.config(bg=BG_HEADER, fg=FG_WHITE, font=self._sf, relief="flat",
                   highlightthickness=0, activebackground=BG_HEADER)
        omb["menu"].config(bg=BG_HEADER, fg=FG_WHITE)
        omb.pack(side="left", padx=(4, 0))

        # Lot-létra tipp (a részleges záráshoz ≥2× min_lot kell)
        _ml = (self.cfg.get("pairs", {}).get(self.symbol, {}) or {}).get("min_lot", 0.01)
        tk.Label(popup, text=f"(A Felező/Pajzs részleges záráshoz ≥2× min_lot ({_ml}) "
                             f"kell; kisebbnél Risky/BE-re esik vissza. A Backtest a "
                             f"ténylegesen alkalmazott technikát mutatja.)",
                 bg=BG, fg=FG_GRAY_DIM, font=self._sf, justify="left",
                 wraplength=560).pack(anchor="w", padx=10, pady=(1, 0))

        # ── Backtest-eredmény sor (a Backtest gomb tölti) ───────────────────
        self.lbl_bt = tk.Label(popup, text="", bg=BG, fg=FG_GRAY_DIM, font=self._sf,
                               justify="left", wraplength=560)
        self.lbl_bt.pack(anchor="w", padx=10, pady=(0, 2))

        # ── Gombsor ─────────────────────────────────────────────────────────
        # EGYETLEN Mentés: elmenti az órákat + a paramétereket (aktív készlet),
        # és ha ez a paraméter-kombináció még nincs a trials CSV-ben, ODA IS
        # beírja — kötelezően backtest-eredménnyel (ha nincs friss eredmény, a
        # Mentés magától lefuttatja a backtestet, majd ment). A régi „Ment új
        # sorszámként" így feleslegessé vált (a CSV-be írás automatikus).
        btns = tk.Frame(popup, bg=BG)
        btns.pack(pady=10)
        self._btn_save = tk.Button(btns, text="Mentés", bg=BTN_PLAY_BG,
                                   fg=BTN_PLAY_FG, relief="flat", font=self._sf,
                                   command=self._save)
        self._btn_save.pack(side="left", padx=6)
        self._btn_bt = tk.Button(btns, text="Backtest", bg=BTN_BT_BG, fg=BTN_BT_FG,
                                 relief="flat", font=self._sf,
                                 command=self._open_backtest_window)
        self._btn_bt.pack(side="left", padx=6)
        tk.Button(btns, text="Trials CSV", bg=BTN_BT_BG, fg=BTN_BT_FG, relief="flat",
                  font=self._sf, command=self._open_trials).pack(side="left", padx=6)
        tk.Button(btns, text="Mégse", bg=BTN_DIS_BG, fg=BTN_DIS_FG, relief="flat",
                  font=self._sf, command=popup.destroy).pack(side="left", padx=6)

    # ── EGYETLEN metrika-sáv renderelése ────────────────────────────────────
    # (label, érték-formázó, metrika-kulcs vagy None). A None kulcs = fehér
    # (semleges) szín; egyébként a metric_colors szemantikus színe.
    _METRIC_ORDER = [
        ("Trade ", lambda s: str(int(s.get("trades", 0))), None),
        ("Win ",   lambda s: f"{s.get('win_rate', 0) * 100:.0f}%", "win_rate"),
        ("MaxDD ", lambda s: f"{s.get('max_drawdown', 0) * 100:.1f}%", "max_drawdown"),
        ("P&L ",   lambda s: f"{s.get('total_pnl', 0):+.0f}$", "total_pnl"),
        ("PF ",    lambda s: (f"{s.get('profit_factor', 0):.2f}"
                              if s.get('profit_factor', 0) != float('inf') else "∞"),
         "profit_factor"),
    ]

    def _render_metrics(self, summary, source: str):
        """A metrika-sáv frissítése a betöltött paraméterkészlet eredményével.

        summary=None → nincs eredmény; trades==0 → 0-trade jelzés. `source` a
        forrás rövid megnevezése (mentett / #N sor / friss backtest)."""
        for w in self._metrics_frame.winfo_children():
            w.destroy()
        self._src_lbl.config(text=(f"forrás: {source}" if source else ""))
        if not summary or summary.get("trades", 0) == 0:
            self._grade_lbl.config(text="Minősítés: —", fg=FG_GRAY)
            if summary is not None and summary.get("trades", 0) == 0:
                tk.Label(self._metrics_frame, text="0 trade ezen a paraméterezésen",
                         bg=BG, fg=FG_YELLOW, font=self._sf).pack(side="left")
            return
        gtxt, gcol, greason = self.strategy.grade(summary, self.cfg)
        self._grade_lbl.config(
            text=f"Minősítés: {gtxt}" + (f"   ({greason})" if greason else ""),
            fg=sem_color(gcol))
        mc = metric_colors(summary, self.cfg)
        for label, fn, key in self._METRIC_ORDER:
            color = "white" if key is None else mc.get(key, "white")
            cell = tk.Frame(self._metrics_frame, bg=BG)
            cell.pack(side="left", padx=(0, 12))
            tk.Label(cell, text=label, bg=BG, fg=FG_GRAY,
                     font=self._sf).pack(side="left")
            tk.Label(cell, text=fn(summary), bg=BG, fg=sem_color(color),
                     font=self._sf).pack(side="left")

    def _summary_from_row(self, row: dict):
        """Egy trials-CSV sorból metrika-összegzés a minősítéshez/megjelenítéshez.
        None, ha a sorban nincs értelmezhető backtest-eredmény."""
        summ = {
            "trades":        int(_num(row.get("trades")) or 0),
            "total_pnl":     _num(row.get("total_pnl")) or 0.0,
            "win_rate":      _num(row.get("win_rate")) or 0.0,
            "profit_factor": _num(row.get("profit_factor")) or 0.0,
            "max_drawdown":  _num(row.get("max_drawdown")) or 0.0,
        }
        if summ["trades"] == 0 and not any(row.get(c) for c in ("win_rate", "total_pnl")):
            return None
        return summ

    def _invalidate_bt(self):
        """Kézi paraméter-átírásnál a friss backtest-eredmény elavul."""
        if self._bt_summary is not None:
            self._bt_summary = None
            self._render_metrics(
                None, "paraméter módosítva — a Mentés lefuttatja a backtestet")

    # ── Sorszám-választó (minőségi rangsor) ─────────────────────────────────
    def _build_rank_selector(self, popup):
        best, worst = self._ranks[0], self._ranks[-1]
        opt_ranks = [r for r in self._ranks if r < _MANUAL_RANK_BASE]
        man_ranks = [r for r in self._ranks if r >= _MANUAL_RANK_BASE]

        bar = tk.Frame(popup, bg=BG)
        bar.pack(anchor="w", padx=10, pady=(2, 0))
        tk.Label(bar, text="Sorszám (minőség, 1 = legjobb):", bg=BG, fg=FG_GRAY,
                 font=self._sf).pack(side="left")

        self.rank_var = tk.StringVar(value="")
        ent = tk.Entry(bar, width=5, textvariable=self.rank_var, bg=BG_HEADER,
                       fg=FG_WHITE, font=self._sf, insertbackground=FG_WHITE,
                       justify="center")
        ent.pack(side="left", padx=(4, 2))
        ent.bind("<Return>", lambda e: self._load_current_rank())

        tk.Button(bar, text="▲", width=2, bg=BG_HEADER, fg=FG_WHITE, relief="flat",
                  font=self._sf, cursor="hand2",
                  command=lambda: self._step_rank(-1)).pack(side="left", padx=1)
        tk.Button(bar, text="▼", width=2, bg=BG_HEADER, fg=FG_WHITE, relief="flat",
                  font=self._sf, cursor="hand2",
                  command=lambda: self._step_rank(+1)).pack(side="left", padx=1)
        tk.Button(bar, text="Betölt", bg=BG_HEADER, fg=FG_WHITE, relief="flat",
                  font=self._sf, cursor="hand2",
                  command=self._load_current_rank).pack(side="left", padx=(4, 0))

        avail = f"Elérhető: 1–{max(opt_ranks)}" if opt_ranks else "Elérhető: —"
        if man_ranks:
            avail += f"  (kézi: {', '.join(str(r) for r in man_ranks)})"
        tk.Label(bar, text=avail, bg=BG, fg=FG_GRAY_DIM,
                 font=self._sf).pack(side="left", padx=(8, 0))

        # Az adott sorszámhoz tartozó metrikák
        self.lbl_rank = tk.Label(popup, text="Válassz sorszámot a betöltéshez.",
                                 bg=BG, fg=FG_GRAY_DIM, font=self._sf)
        self.lbl_rank.pack(anchor="w", padx=10, pady=(1, 2))

    def _step_rank(self, direction: int):
        """▲ = jobb (kisebb sorszám felé), ▼ = rosszabb — az elérhető ranksoron."""
        cur = _num(self.rank_var.get())
        if cur is None:
            target = self._ranks[0]
        else:
            cur = int(cur)
            after = [r for r in self._ranks if (r > cur if direction > 0 else r < cur)]
            if not after:
                return
            target = after[0] if direction > 0 else after[-1]
        self.rank_var.set(str(target))
        self._load_rank(target)

    def _load_current_rank(self):
        r = _num(self.rank_var.get())
        if r is None:
            self.lbl_rank.config(text="Érvénytelen sorszám.", fg=FG_RED)
            return
        self._load_rank(int(r))

    def _load_rank(self, rank: int):
        row = self._rank_rows.get(rank)
        if row is None:
            self.lbl_rank.config(
                text=f"Nincs {rank}. sorszámú sor (elérhető: "
                     f"{self._ranks[0]}–{self._ranks[-1]}).", fg=FG_RED)
            return
        for k, e in self.entries.items():
            if k in row:
                e.delete(0, "end")
                e.insert(0, self._fmt_param(k, row[k]))
        # Betöltéskor a korábbi friss backtest már nem erre a készletre vonatkozik.
        self._bt_summary = None
        note = (row.get("note") or "").strip()
        summ = self._summary_from_row(row)
        if summ is None:
            self._render_metrics(None, f"#{rank} sor — nincs mentett metrika")
            self.lbl_rank.config(
                text=f"#{rank} betöltve" + (f" ({note})" if note else "")
                     + " — nincs mentett metrika.", fg=FG_GRAY)
        else:
            src = f"#{rank} sor (trials CSV)" + (f", {note}" if note else "")
            self._render_metrics(summ, src)
            self.lbl_rank.config(text=f"#{rank} betöltve.", fg=FG_GRAY)

    # ── Óra-rács (trade_hours) ──────────────────────────────────────────────
    def _build_hours(self, popup, ts):
        """A live óra-kapuja a STRATÉGIA-hatókörű órákat nézi (`{symbol}_hours.json`
        a stratégia mappájában), visszaesve a régi config.json szimbólum-szintű
        `trade_hours`-ra. Az óránkénti P&L (az optimalizált test_summary-ből) segít
        eldönteni, mely órákat vegyük ki (a mínuszosakat kézzel kikattintva). A
        bepipált órákat az EGYETLEN Mentés gomb menti a stratégia óra-fájljába."""
        params = self._src
        hp_raw = (ts or {}).get("hourly_pnl", {})
        hourly = {}
        for _k, _v in hp_raw.items():
            try:
                hourly[int(_k)] = _v
            except (ValueError, TypeError):
                pass

        _pc = self.cfg.get("pairs", {}).get(self.symbol, {})
        _cur = resolve_trade_hours(self.symbol, self.strategy.name,
                                   _pc.get("trade_hours"))
        if _cur is not None:
            _checked0 = {int(h) for h in _cur}
        else:
            _hs, _he = params.get("trade_hour_start"), params.get("trade_hour_end")
            if isinstance(_hs, (int, float)) and isinstance(_he, (int, float)):
                _checked0 = {h for h in range(24) if int(_hs) <= h < int(_he)}
            else:
                _checked0 = set(range(24))

        tk.Label(popup, text="Kereskedési órák (szerver/chart idő) — pipáld be, mely órákban "
                             "kereskedjen (óránkénti P&L az optimalizálásból):",
                 bg=BG, fg=FG_GRAY, font=self._sf).pack(anchor="w", padx=10, pady=(8, 0))

        hours_frame = tk.Frame(popup, bg=BG)
        hours_frame.pack(anchor="w", padx=10, pady=(2, 2))
        hour_on = {h: (h in _checked0) for h in range(24)}
        hour_btns = {}

        def _paint(h):
            btn = hour_btns[h]
            if hour_on[h]:
                btn.config(bg=FG_GREEN, fg="#1e1e2e")     # BE — zöld
            else:
                btn.config(bg=BG_HEADER, fg=FG_GRAY_DIM)  # KI — sötét

        def _toggle(h):
            hour_on[h] = not hour_on[h]
            _paint(h)

        for h in range(24):
            colf = tk.Frame(hours_frame, bg=BG)
            colf.grid(row=0, column=h, padx=1)
            btn = tk.Label(colf, text=f"{h:02d}", width=2, padx=2, pady=2,
                           font=("Courier New", 8, "bold"), cursor="hand2")
            btn.pack()
            btn.bind("<Button-1>", lambda e, hh=h: _toggle(hh))
            hour_btns[h] = btn
            _paint(h)
            _b = hourly.get(h)
            if _b:
                _pnl, _cnt = _b.get("pnl", 0.0), _b.get("count", 0)
                tk.Label(colf, text=f"{_pnl:+.0f}", bg=BG,
                         fg=FG_GREEN if _pnl >= 0 else FG_RED,
                         font=("Courier New", 7)).pack()
                tk.Label(colf, text=f"{_cnt}", bg=BG, fg=FG_GRAY,
                         font=("Courier New", 7)).pack()
            else:
                tk.Label(colf, text="—", bg=BG, fg=FG_GRAY_DIM,
                         font=("Courier New", 7)).pack()
                tk.Label(colf, text="", bg=BG, font=("Courier New", 7)).pack()

        # Az óraállapotot az EGYETLEN Mentés gomb olvassa ki és menti (nincs külön
        # „Órák mentése" gomb). Az „Auto-javasol" is elmaradt: az óránkénti P&L jól
        # látható a rácsban, így a mínuszos órák kézzel kikattinthatók.
        self._hour_on = hour_on

    # ── Mentés ──────────────────────────────────────────────────────────────
    def _collect_params(self):
        """Az Entry-k tartalma → típusos paraméter-dict. Hiba esetén None."""
        new_params = {k: v for k, v in self._src.items() if not k.startswith("_")}
        for k, e in self.entries.items():
            v = e.get().strip()
            orig = self._types.get(k)
            try:
                if isinstance(orig, bool):
                    new_params[k] = v.lower() in ("true", "1", "igen", "yes")
                elif isinstance(orig, int):
                    new_params[k] = int(float(v))
                elif isinstance(orig, float):
                    new_params[k] = float(v)
                else:
                    # Nincs típus-minta (pl. tisztán CSV-ből jött kulcs): próbáljunk
                    # számot, különben szöveg.
                    fv = _num(v)
                    new_params[k] = fv if (fv is not None and v != "") else v
            except ValueError:
                self.lbl_err.config(text=f"Hibás érték: {k} = {v!r}")
                return None
        return new_params

    def _write_json(self, new_params: dict, extra: dict | None = None) -> bool:
        data = dict(self.data) if self.data else {"symbol": self.symbol}
        data["params"] = new_params
        data["manually_edited_at"] = datetime.utcnow().isoformat()
        if self.is_new and "source" not in data:
            data["source"] = "manual"
        # Ha futott Backtest, a friss összegzés kerül a JSON-ba → a soron
        # megjelenik a minősítés (Win/MaxDD/P&L a test_summary-ből).
        if self._bt_summary is not None:
            data["test_summary"] = self._bt_summary
            data["backtested_at"] = datetime.utcnow().isoformat()
        if extra:
            data.update(extra)
        try:
            tmp = self.pf.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False, default=str)
            tmp.replace(self.pf)
        except Exception as ex:
            self.lbl_err.config(text=f"Mentési hiba: {ex}")
            return False
        self.data = data
        self.is_new = False
        # A chart-viz a friss JSON-paramétert olvassa → CLEAR + azonnali újrarajz,
        # hogy a TradeForgeViz a KÖVETKEZŐ ciklusban az új paraméterekkel rajzoljon,
        # ÉS a régi (elavult) belépő-jelzések egy atomi írásban eltűnjenek (nem kell
        # a V-t ki/be kapcsolni). Csak ha fut a live loop.
        try:
            from trading import live_trader as _lt
            _lt.request_viz_clear(self.symbol)
        except Exception:
            pass
        return True

    def _save_hours(self) -> int:
        """A bepipált órák mentése a STRATÉGIA óra-fájljába
        (`data/optimized_params/<strategy>/<symbol>_hours.json`) — NEM a
        config.json-ba, így minden stratégiának SAJÁT órái lehetnek ugyanazon az
        instrumentumon. Visszaadja a kiválasztott órák számát."""
        sel = [h for h in range(24) if self._hour_on.get(h)]
        save_trade_hours(self.symbol, sel, self.strategy.name)
        # A live loop és a viz a stratégia óra-fájlját olvassa (feloldó) → a
        # következő ciklusban azonnal él. CLEAR + azonnali újrarajz, hogy AZONNAL az
        # új órákkal rajzoljon és a régi jelzések eltűnjenek. Csak ha fut a live loop.
        try:
            from trading import live_trader as _lt
            _lt.request_viz_clear(self.symbol)
        except Exception:
            pass
        return len(sel)

    def _save(self):
        """EGYETLEN Mentés: órák + paraméterek (aktív készlet) + trials CSV (ha a
        kombó még nincs benne), KÖTELEZŐEN backtest-eredménnyel. Ha új a kombó és
        nincs friss eredmény → előbb lefuttatja a backtestet (a _bt_done folytatja
        a mentést); egyébként azonnal ír."""
        params = self._collect_params()
        if params is None:
            return
        dup = self._find_matching_rank(params) if self._rank_rows else None
        if dup is None and self._bt_summary is None:
            # Új kombó, nincs eredmény → kötelező backtest, utána _persist.
            self._save_after_bt = True
            self.lbl_err.config(text="")
            self._run_backtest()
            return
        self._persist(params, dup)

    def _persist(self, params: dict, dup):
        """A tényleges kiírás: órák + JSON (aktív készlet) + trials CSV (ha új kombó
        és van érdemi eredmény). `dup` = a megegyező sorszám vagy None."""
        try:
            self._save_hours()
        except Exception as ex:
            self.lbl_err.config(text=f"Óra-mentési hiba: {ex}", fg=FG_RED)
            return
        # A JSON test_summary: friss backtest, vagy a megegyező sor mentett metrikái.
        if self._bt_summary is None and dup is not None:
            self._bt_summary = self._summary_from_row(self._rank_rows.get(dup, {}))
        extra = None
        has_result = bool(self._bt_summary) and self._bt_summary.get("trades", 0) > 0
        if dup is None and has_result:
            # Új kombó → felvesszük a trials CSV-be (eredménnyel), hogy visszatölthető.
            new_rank = _MANUAL_RANK_BASE
            while new_rank in self._rank_rows:
                new_rank += 1
            try:
                self._append_manual_trial(new_rank, params, self._bt_summary)
            except Exception as ex:
                self.lbl_err.config(text=f"CSV-mentési hiba: {ex}", fg=FG_RED)
                return
            rec = {k: str(v) for k, v in params.items()}
            rec.update({"rank": str(new_rank), "note": "manual"})
            for mk in ("trades", "win_rate", "total_pnl", "profit_factor", "max_drawdown"):
                rec[mk] = str(self._bt_summary.get(mk, ""))
            self._rank_rows[new_rank] = rec
            self._ranks = sorted(self._rank_rows)
            extra = {"manual_rank": new_rank}
        if not self._write_json(params, extra=extra):
            return
        self.popup.destroy()

    _METRIC_SAVE_COLS = ("trades", "win_rate", "total_pnl", "profit_factor",
                         "max_drawdown")

    def _append_manual_trial(self, rank: int, params: dict, summary: dict | None = None):
        """Egy kézi paraméter-sor hozzáfűzése a trials CSV-hez, `rank` oszloppal +
        (ha van) a backtest-eredmény metrika-oszlopaival.

        pandas-szal olvassuk/írjuk vissza (magyar ';'+','), így ha a régi CSV-ben
        még nincs `rank` oszlop, most bekerül (a sor pozíciója szerint 1…N)."""
        import pandas as pd
        if self.trials_csv.exists():
            df = pd.read_csv(self.trials_csv, sep=";", decimal=",",
                             encoding="utf-8-sig")
        else:
            df = pd.DataFrame()
        if df.empty:
            # Nincs még CSV → fejléc a paraméterekből + metrikákból, hogy az érték
            # tényleg elmentődjön (üres df-nél nem lenne oszlop, amibe írjunk).
            cols = ["rank"] + list(params.keys()) + list(self._METRIC_SAVE_COLS) + ["note"]
            df = pd.DataFrame(columns=cols)
        if "rank" not in df.columns:
            df.insert(0, "rank", range(1, len(df) + 1))
        row = {c: "" for c in df.columns}
        row["rank"] = rank
        if "note" in df.columns:
            row["note"] = "manual"
        for k, v in params.items():
            if k in df.columns:
                row[k] = v
            # Ha a paraméter-oszlop hiányzik a CSV-ből, kihagyjuk (ne torzítsuk
            # a fejlécet — a betöltés úgyis csak a meglévő oszlopokat használja).
        if summary:
            for mk in self._METRIC_SAVE_COLS:
                if mk in df.columns:
                    row[mk] = summary.get(mk, "")
        new_df = pd.DataFrame([row], columns=list(df.columns))
        df = new_df if len(df) == 0 else pd.concat([df, new_df], ignore_index=True)
        df.to_csv(self.trials_csv, sep=";", decimal=",", index=False,
                  encoding="utf-8-sig")

    def _open_trials(self):
        if not self.trials_csv.exists():
            self.lbl_err.config(text="Nincs trials CSV — futtass optimalizálást előbb.")
            return
        try:
            import os
            os.startfile(str(self.trials_csv))   # Windows: alap app (Excel)
        except Exception as ex:
            self.lbl_err.config(text=f"Megnyitási hiba: {ex}")

    # ── Duplikátum-keresés a rangsorban ─────────────────────────────────────
    def _find_matching_rank(self, params: dict):
        """Van-e már olyan sorszám, aminek a szerkeszthető paraméterei (az űrlap
        mezői) numerikusan megegyeznek a `params`-szal? Visszaad rangot vagy None."""
        import math
        for rank in sorted(self._rank_rows):
            row = self._rank_rows[rank]
            ok = True
            for k in self.entries:
                rv = _num(row.get(k))
                pv = params.get(k)
                if rv is None or pv is None:
                    ok = False
                    break
                try:
                    if not math.isclose(float(rv), float(pv), rel_tol=1e-9, abs_tol=1e-6):
                        ok = False
                        break
                except (TypeError, ValueError):
                    ok = False
                    break
            if ok:
                return rank
        return None

    # ── Kockázatcsökkentés preset (per-pár) ─────────────────────────────────
    def _preset_from_name(self, name: str) -> str:
        return {v: k for k, v in self._rrs.NAME.items()}.get(name, self._rrs.PRESET_OFF)

    def _runner_from_name(self, name: str) -> str:
        return {v: k for k, v in self._rrs.RUNNER_NAME.items()}.get(
            name, self._rrs.RUNNER_TRAILING)

    def _on_rr_change(self, name: str):
        """A választott preset mentése a per-pár állapotba (data/risk_mode.json).
        A régi risky_mode-ot szinkronban tartjuk (preset==risky), mint a sor R gombja."""
        preset = self._preset_from_name(name)
        self._rrs.set_preset(self.symbol, preset)
        try:
            from core import risky_mode, risk_reduction as _rr
            risky_mode.set_risky(self.symbol, preset == _rr.PRESET_RISKY)
        except Exception:
            pass

    def _on_cautious_change(self):
        self._rrs.set_cautious(self.symbol, bool(self._cautious_var.get()))

    def _on_runner_change(self, name: str):
        self._rrs.set_runner(self.symbol, self._runner_from_name(name))

    def _on_exit_ind_change(self, name: str):
        ind = {v: k for k, v in self._EXIND_NAME.items()}.get(name, "supertrend")
        self._rrs.set_exit_config(self.symbol, indicator=ind)
        self._rebuild_exit_params()

    def _on_build_mode_change(self, name: str):
        mode = {v: k for k, v in self._bst.NAME.items()}.get(name, self._bst.MODE_OFF)
        self._bst.set_mode(self.symbol, mode)

    def _rebuild_exit_params(self):
        """Az exit-indikátor SZERKESZTHETŐ paraméter-mezőinek újraépítése (a kiválasztott
        indikátor szerint), a per-pár exit-configból feltöltve."""
        for w in self._exit_pfrm.winfo_children():
            w.destroy()
        self._exit_param_vars = {}
        ind = {v: k for k, v in self._EXIND_NAME.items()}.get(self._exit_ind_name.get(), "supertrend")
        cfg = self._rrs.get_exit_config(self.symbol)
        for key, label in self._EXIT_PARAM_SPEC.get(ind, []):
            tk.Label(self._exit_pfrm, text=f"{label}:", bg=BG, fg=FG_GRAY,
                     font=self._sf).pack(side="left")
            var = tk.StringVar(value=str(cfg.get(key, "")))
            e = tk.Entry(self._exit_pfrm, textvariable=var, width=(5 if key == "osc" else 4),
                         bg=BG_HEADER, fg=FG_WHITE, font=self._sf, relief="flat",
                         insertbackground=FG_WHITE)
            e.pack(side="left", padx=(2, 6))
            e.bind("<FocusOut>", lambda ev, k=key: self._save_exit_param(k))
            e.bind("<Return>",   lambda ev, k=key: self._save_exit_param(k))
            self._exit_param_vars[key] = var

    def _save_exit_param(self, key: str):
        """Egy exit-paraméter mentése a per-pár configba (típus-validálással)."""
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
        self._rrs.set_exit_config(self.symbol, **{key: val})

    def _rr_spec_from_ui(self):
        """A UI-ban beállított teljes spec (preset + óvatos méret + runner + exit).
        None, ha 'Ki' (→ a run_pair az alap OFF viselkedést futtatja)."""
        from core import risk_reduction as _rr
        preset = self._preset_from_name(self._rr_name.get())
        if preset == _rr.PRESET_OFF:
            return None
        runner = self._runner_from_name(self._runner_name.get())
        exit_cfg = self._rrs.get_exit_config(self.symbol)
        exit_cfg["enabled"] = (runner == _rr.RUNNER_EXIT)   # a UI runner-választása dönt
        return {**_rr.default_config(), "preset": preset,
                "runner_stop": runner,
                "cautious": bool(self._cautious_var.get()),
                "exit": exit_cfg}

    # ── Backtest önálló ablak (progress + időszak + élő egyenleg) ────────────
    def _open_backtest_window(self):
        """A „Backtest" gomb a szabványos B3 ablakot nyitja (állítható időszak,
        progress bar, élő egyenleg/kötések/technika). Az eredményt visszaadja ide
        (metrika-sáv + a Mentés is látja)."""
        params = self._collect_params()
        if params is None:
            return
        pair_cfg = self.cfg.get("pairs", {}).get(self.symbol)
        if not isinstance(pair_cfg, dict):
            self.lbl_bt.config(text="Nincs pár-config ehhez az instrumentumhoz.",
                               fg=FG_RED)
            return
        from dashboard.backtest_dialog import BacktestDialog
        BacktestDialog(
            self.popup, self.symbol, self.cfg, self.strategy, params, pair_cfg,
            self._rr_spec_from_ui(), self._hf, self._sf,
            on_result=self._on_bt_window_result,
            preset_name=self._rr_name.get(),
            on_apply_params=self._apply_params_from_bt)

    def _on_bt_window_result(self, summary):
        """A Backtest-ablak végeredménye → a közös metrika-sávba + a Mentés forrása."""
        self._bt_summary = summary
        self._render_metrics(summary, "friss backtest")

    def _apply_params_from_bt(self, params: dict, summary=None):
        """A Backtest-ablak „Mentés a Paraméterekhez" gombja → a (feltáró) paraméterek
        visszaírása EBBE az űrlapba (a lemezre mentést a Mentés gomb végzi). Ha van
        friss backtest-eredmény, azt is átvesszük forrásként."""
        for k, e in self.entries.items():
            if k in params:
                e.delete(0, "end")
                e.insert(0, self._fmt_param(k, params[k]))
        if summary and summary.get("trades", 0) > 0:
            self._bt_summary = summary
            self._render_metrics(summary, "friss backtest (a Backtest-ablakból)")
        else:
            self._bt_summary = None
            self._render_metrics(
                None, "paraméterek a Backtest-ablakból — a Mentés lefuttatja a backtestet")

    # ── Backtest inline (a Mentés auto-útja: gyors, ablak nélkül) ────────────
    def _run_backtest(self):
        if self._bt_running:
            return
        params = self._collect_params()
        if params is None:
            return
        pair_cfg = self.cfg.get("pairs", {}).get(self.symbol)
        if not isinstance(pair_cfg, dict):
            self.lbl_bt.config(text="Nincs pár-config ehhez az instrumentumhoz.", fg=FG_RED)
            return
        self._bt_running = True
        rr_spec = self._rr_spec_from_ui()          # a választott preset (vagy None)
        self._btn_bt.config(text="Backtest fut…", state="disabled")
        # Futás alatt a Mentés is tiltva (az auto-mentés amúgy is a végén folytatódik).
        try:
            self._btn_save.config(state="disabled")
        except Exception:
            pass
        _pname = self._rr_name.get()
        _saving = " — mentés a végén" if self._save_after_bt else ""
        self.lbl_bt.config(text=f"Backtest fut (teljes hist., {_pname}){_saving} — kis türelmet…",
                           fg=FG_GRAY)

        def work():
            summary, err = None, None
            try:
                from trading.backtest import load_data, run_pair
                df15, df1 = load_data(self.symbol)
                if df15 is None:
                    err = "Nincs letöltött adat (data/m15, data/m1) ehhez a párhoz."
                else:
                    ib = float(self.cfg.get("ml", {}).get("starting_balance_eur", 1000.0))
                    res = run_pair(self.symbol, df15, df1, params, pair_cfg,
                                   self.cfg["trading"], ib, strategy=self.strategy,
                                   rr=rr_spec)
                    summary = res.summary(ib)
                    # A ténylegesen alkalmazott technikák (lot-létra hatása)
                    from collections import Counter
                    tech = Counter(t.rr_technique for t in res.closed
                                   if getattr(t, "rr_technique", ""))
                    if summary and tech:
                        summary["_rr_tech"] = dict(tech)
            except Exception as ex:
                err = str(ex)
            try:
                self.popup.after(0, lambda: self._bt_done(summary, err))
            except Exception:
                pass

        threading.Thread(target=work, daemon=True, name="InstrBacktest").start()

    def _bt_done(self, summary, err):
        self._bt_running = False
        try:
            self._btn_bt.config(text="Backtest", state="normal")
            self._btn_save.config(state="normal")
        except Exception:
            return   # a popup közben bezárult
        # Volt-e függő (auto-)mentés? Elfogyasztjuk, majd a végén folytatjuk.
        pending = self._save_after_bt
        self._save_after_bt = False
        if err:
            self.lbl_bt.config(text=f"Backtest hiba: {err}", fg=FG_RED)
            self._render_metrics(None, "backtest hiba")
            return
        # A metrikák a KÖZÖS sávba kerülnek (nincs külön backtest-metrikasor). Az
        # lbl_bt már csak a ténylegesen alkalmazott kockázati technikát mutatja.
        tech = (summary or {}).pop("_rr_tech", None) or {}
        _names = {"shield": "Pajzs", "halving": "Felező", "risky": "Risky"}
        tech_s = (", ".join(f"{_names.get(k, k)}×{v}" for k, v in tech.items())) if tech else ""
        self._bt_summary = summary or {"trades": 0}
        self._render_metrics(self._bt_summary, "friss backtest")
        self.lbl_bt.config(
            text=(f"Ténylegesen alkalmazott technika: {tech_s}" if tech_s else ""),
            fg=FG_GRAY_DIM)
        if pending:
            # Auto-mentés folytatása: a friss eredménnyel most már perzisztálunk.
            params = self._collect_params()
            if params is not None:
                dup = self._find_matching_rank(params) if self._rank_rows else None
                self._persist(params, dup)
