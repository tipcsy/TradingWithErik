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
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dashboard.theme import (
    BG, BG_HEADER, BG_ROW_ODD, BG_ROW_EVEN, BG_INACTIVE, BG_UNTRAINED,
    BG_OPT_ROW, BG_BT,
    FG_WHITE, FG_GREEN, FG_RED, FG_YELLOW, FG_GRAY, FG_GRAY_DIM, FG_BLUE,
    FG_CYAN, FG_ORANGE, FG_PURPLE, FG_TEAL,
    BTN_PLAY_BG, BTN_PLAY_FG, BTN_STOP_BG, BTN_STOP_FG, BTN_OPT_BG, BTN_OPT_FG,
    BTN_BT_BG, BTN_BT_FG, BTN_DIS_BG, BTN_DIS_FG,
    CANVAS_BG, CANVAS_LINE, CANVAS_REF,
    color as sem_color,
)
from strategy import get_strategy
from strategy.base import Column
from strategy.settings import apply_strategy_config, main_config_view
from core import risky_mode
from version import APP_NAME, APP_VERSION


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
    Column("market",   "Piac",       10, "center", kind="fixed"),
    Column("quality",  "Minőség",     9, "center", kind="fixed"),
    Column("opt",      "Opt státusz",18, "w",      kind="fixed"),
]


def opt_done_date(symbol: str, strategy_name: str):
    """Az ADOTT stratégia utolsó optimalizálásának ideje a done-marker fájlból
    (`{symbol}_study.done`, a stratégia mappájában), vagy None ha nincs marker."""
    try:
        from core.params_store import done_marker
        import datetime as _dt
        dm = done_marker(symbol, strategy_name)
        if dm.exists():
            return _dt.datetime.fromtimestamp(dm.stat().st_mtime)
    except Exception:
        pass
    return None


def opt_done_label(symbol: str, strategy_name: str) -> str:
    """PERZISZTENS 'utolsó optimalizálás' címke EGY stratégiára, pl.
    'Utolsó opt: 26/07/16'. '' ha nincs marker. Modul-szintű (a vezérlő és az
    ablak is használja — az OptimizerController-nek nincs `strategy` tagja)."""
    d = opt_done_date(symbol, strategy_name)
    return f"Utolsó opt: {d.strftime('%y/%m/%d')}" if d else ""


def build_columns(strategies) -> list[Column]:
    """A teljes oszloplista: fix elöl + stratégiánként a középső oszlopok + fix hátul.

    `strategies`: egy Strategy VAGY Strategy-lista (több-stratégia). Minden stratégia
    jelölő-oszlopa a stratégia nevét kapja fejlécnek + egyedi kulcsot + strategy_name-t
    (így a per-instrumentum be/ki és a per-stratégia cellák szétválaszthatók)."""
    from dataclasses import replace as _replace
    if not isinstance(strategies, (list, tuple)):
        strategies = [strategies]
    mid: list[Column] = []
    for st in strategies:
        for col in st.columns():
            if col.kind == "marker":
                mid.append(_replace(col, key=f"{col.key}_{st.name}",
                                    header=st.name, strategy_name=st.name))
            else:
                mid.append(col)
    # A TF-együttállás („Együtt") oszlop a stratégia-oszlopok ELÉ kerül.
    tfalign = [Column("tfalign", "Együtt", 10, "center", kind="tfalign")]
    return LEADING_COLUMNS + tfalign + mid + TRAILING_COLUMNS


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
    if key == "market":
        # Piac-előszűrő aktuális állapota (ha van kiválasztva); egyébként „—".
        if getattr(ds, "market_strategy", None):
            return (getattr(ds, "market_state_label", "") or "—",
                    getattr(ds, "market_state_color", "muted"))
        return "—", "muted"
    if key == "quality":
        g = getattr(ds, "opt_grade", None)
        if g:
            return g[0], g[1]
        return "—", "muted"
    if key == "opt":
        txt = opt_status or "—"
        if inst_state in ("OPTIMIZING", "QUEUED"):
            col = "yellow" if inst_state == "OPTIMIZING" else "muted"
        else:
            col = "green" if ("Kész" in txt or "Utolsó opt" in txt
                              or txt.startswith("Opt:")) else "muted"
        return txt, col
    return "—", "muted"


# ---------------------------------------------------------------------------
# Live Dashboard — egy sor widgetei (oszlop-vezérelt)
# ---------------------------------------------------------------------------

class PairRow:
    def __init__(self, parent: tk.Frame, symbol: str, row_idx: int, columns: list,
                 on_run, on_opt, on_delete, on_risky, on_name_click, mono_font, small_font,
                 on_status_click=None, on_viz=None, on_marker_click=None, on_opt_menu=None,
                 on_trades=None, on_tfalign=None):
        self.symbol  = symbol
        self.columns = columns
        self._bg     = BG_ROW_ODD if row_idx % 2 == 0 else BG_ROW_EVEN
        self._mono   = mono_font
        self._opt_full = ""       # az Opt státusz TELJES szövege (tooltiphez)
        self._opt_tip  = None     # a lebegő tooltip-ablak (Toplevel), ha látszik

        self.frame = tk.Frame(parent, bg=self._bg)
        # Nem csomagoljuk magát — _apply_filter_sort() kezeli

        self.labels: dict[str, tk.Label] = {}
        # Körös jelölő-oszlopok: col.key → (frame, [(stádium_kulcs, kör-Label), …]).
        # A fix szélességű Frame (pack_propagate ki) igazodik a fejléc-oszlophoz
        # (width karakter × mono px + 2×padx), a körök benne elosztva.
        self.markers: dict[str, tuple] = {}
        self.tfalign = None            # TF-együttállás cella (dots + S), lazán építve
        self._on_tfalign = None        # kattintás-callback a TF-align cellához
        _charpx = mono_font.measure("0")
        _cellh  = mono_font.metrics("linespace") + 6
        for col in self.columns:
            if col.kind == "marker":
                cell = tk.Frame(self.frame, bg=self._bg,
                                width=_charpx * col.width + 8, height=_cellh)
                cell.pack(side="left")
                cell.pack_propagate(False)
                # A körökre kattintva → az adott STRATÉGIA paraméterei (Stratégia
                # Paraméterek ablak). A stratégia neve az oszlopból (col.strategy_name).
                if on_marker_click is not None:
                    cell.config(cursor="hand2")
                    cell.bind("<Button-1>",
                              lambda e, sn=col.strategy_name: on_marker_click(symbol, sn))
                # „Lego-kocka" keret: vékony szegély KÖRBEN (fent/lent is), benne
                # a stratégia körei → a stratégiák jelölő-csoportjai dobozokként
                # különülnek el: ▢● ● ●▢ ▢● ●▢
                inner = tk.Frame(cell, bg=self._bg, highlightthickness=1,
                                 highlightbackground=FG_GRAY_DIM,
                                 highlightcolor=FG_GRAY_DIM)
                inner.pack(expand=True, pady=2)
                _click = (lambda e, sn=col.strategy_name:
                          on_marker_click(symbol, sn)) if on_marker_click else None
                if _click is not None:
                    inner.config(cursor="hand2")
                    inner.bind("<Button-1>", _click)
                # No-trade (⏸) jel KÜLÖN helyen — nem az első kört cseréli le,
                # így az irány-szín (zöld/piros pötty) a szünet alatt is látszik.
                pause = tk.Label(inner, text="", bg=self._bg, fg=FG_GRAY,
                                 font=mono_font, padx=0)
                pause.pack(side="left")
                circles = []
                for skey, _slabel in col.stages:
                    c = tk.Label(inner, text="●", bg=self._bg, fg=FG_GRAY,
                                 font=mono_font, padx=1)
                    if _click is not None:
                        c.config(cursor="hand2")
                        c.bind("<Button-1>", _click)
                    c.pack(side="left", expand=True)
                    circles.append((skey, c))
                self.markers[col.key] = (cell, circles, pause, inner)
                continue
            if col.kind == "tfalign":
                # TF-együttállás cella: idősíkonként egy színes pont (zöld BUY / piros
                # SELL / szürke semleges) + egy erős „S", ha MIND egyezik. A pontokat
                # az első frissítéskor építjük (a figyelt idősíkok számától függően).
                # A cellára kattintva → a TF-együttállás beállítás-ablaka (idősíkok+SMA).
                cell = tk.Frame(self.frame, bg=self._bg,
                                width=_charpx * col.width + 8, height=_cellh)
                cell.pack(side="left")
                cell.pack_propagate(False)
                inner = tk.Frame(cell, bg=self._bg)
                inner.pack(expand=True)
                self._on_tfalign = on_tfalign
                if on_tfalign is not None:
                    _tclick = lambda e: on_tfalign(symbol)
                    for _w in (cell, inner):
                        _w.config(cursor="hand2")
                        _w.bind("<Button-1>", _tclick)
                self.tfalign = {"inner": inner, "dots": [], "s": None}
                continue
            if col.key == "opt":
                # Az Opt státusz a Vezérlés gombok UTÁN jön, és az ablak MARADÉK
                # szélességét tölti ki (lásd lent) → a hosszú szöveg is kifér.
                continue
            lbl = tk.Label(self.frame, text="—", width=col.width, anchor=col.anchor,
                           bg=self._bg, fg=FG_GRAY, font=mono_font, padx=4, pady=3)
            lbl.pack(side="left")
            self.labels[col.key] = lbl

        # A Symbol cellára kattintva → optimalizált paraméterek szerkesztője
        self.labels["symbol"].config(cursor="hand2")
        self.labels["symbol"].bind("<Button-1>", lambda e: on_name_click(symbol))

        # Egy gomb a futtatáshoz (Play↔Stop morph) és egy az OPT-hoz (OPT↔STOP morph).
        # A gombok egy KERETBEN ülnek → a keret tényleges pixel-szélessége adja a
        # fejléc "Vezérlés" cellájának szélességét (pontos oszlop-igazítás).
        self.ctrl_frame = tk.Frame(self.frame, bg=self._bg)
        self.ctrl_frame.pack(side="left")
        self.btn_run = tk.Button(self.ctrl_frame, text="▶", width=3,
                                 bg=BTN_DIS_BG, fg=BTN_DIS_FG, font=small_font,
                                 relief="flat", command=lambda: on_run(symbol))
        self.btn_run.pack(side="left", padx=1)
        self.btn_risky = tk.Button(self.ctrl_frame, text="R", width=2,
                                   bg=BTN_DIS_BG, fg=BTN_DIS_FG, font=small_font,
                                   relief="flat", command=lambda: on_risky(symbol))
        self.btn_risky.pack(side="left", padx=1)
        # Vizualizáció ki/be az adott instrumentumhoz (MT5 chart-rajz)
        self.btn_viz = tk.Button(self.ctrl_frame, text="V", width=2,
                                 bg=BTN_DIS_BG, fg=BTN_DIS_FG, font=small_font,
                                 relief="flat",
                                 command=(lambda: on_viz(symbol)) if on_viz else None)
        self.btn_viz.pack(side="left", padx=1)
        # Jel-replay réteg ki/be a charton (a sűrű zöld/piros belépő-jelzés vonalak +
        # Entry/TP/SL). A tényleges MT5-kötések (nyíl + valós SL/TP) mindig látszanak.
        self.btn_trades = tk.Button(self.ctrl_frame, text="K", width=2,
                                    bg=BTN_DIS_BG, fg=BTN_DIS_FG, font=small_font,
                                    relief="flat",
                                    command=(lambda: on_trades(symbol)) if on_trades else None)
        self.btn_trades.pack(side="left", padx=1)
        self.btn_opt = tk.Button(self.ctrl_frame, text="OPT", width=4,
                                 bg=BTN_DIS_BG, fg=BTN_DIS_FG, font=small_font,
                                 relief="flat", command=lambda: on_opt(symbol))
        self.btn_opt.pack(side="left", padx=1)
        # JOBB-klikk az OPT-on → konkrét stratégia választása (több-stratégiás eset)
        if on_opt_menu is not None:
            self.btn_opt.bind("<Button-3>", lambda e: on_opt_menu(symbol, e))
        self.btn_del = tk.Button(self.ctrl_frame, text="✕", width=2,
                                 bg=BTN_DIS_BG, fg=BTN_DIS_FG, font=small_font,
                                 relief="flat", command=lambda: on_delete(symbol))
        self.btn_del.pack(side="left", padx=(1, 4))

        # ── Opt státusz — a Vezérlés UTÁN, az ablak MARADÉK szélességében ────
        # (fill+expand → a hosszú státusz-szöveg is kifér; a tooltip marad,
        # hátha nagyon keskeny az ablak).
        _opt_col = next((c for c in self.columns if c.key == "opt"), None)
        if _opt_col is not None:
            lbl = tk.Label(self.frame, text="—", width=_opt_col.width,
                           anchor=_opt_col.anchor, bg=self._bg, fg=FG_GRAY,
                           font=mono_font, padx=4, pady=3)
            lbl.pack(side="left", fill="x", expand=True)
            self.labels["opt"] = lbl
            # Kattintás → részletes állapot / hibalog / trials CSV; hover → teljes
            # szöveg tooltipben.
            if on_status_click:
                lbl.config(cursor="hand2")
                lbl.bind("<Button-1>", lambda e: on_status_click(symbol))
            lbl.bind("<Enter>", self._opt_tip_show)
            lbl.bind("<Leave>", self._opt_tip_hide)

    def _morph_btn(self, btn, text, enabled, active_bg, active_fg):
        if enabled:
            btn.config(text=text, bg=active_bg, fg=active_fg, state="normal")
        else:
            btn.config(text=text, bg=BTN_DIS_BG, fg=BTN_DIS_FG, state="disabled")

    # ── Opt státusz tooltip (a keskeny cellában elcsúszó teljes szöveg) ──────
    def _opt_tip_show(self, event):
        text = (self._opt_full or "").strip()
        if not text or text == "—" or self._opt_tip is not None:
            return
        lbl = self.labels.get("opt")
        tip = tk.Toplevel(lbl)
        tip.wm_overrideredirect(True)       # keret nélküli buborék
        tip.attributes("-topmost", True)
        x = lbl.winfo_rootx()
        y = lbl.winfo_rooty() + lbl.winfo_height() + 2
        tk.Label(tip, text=text, bg="#2a2a3a", fg="#e0e0f0",
                 font=self._mono, padx=6, pady=3, relief="solid", bd=1,
                 justify="left").pack()
        tip.wm_geometry(f"+{x}+{y}")
        self._opt_tip = tip

    def _opt_tip_hide(self, event=None):
        if self._opt_tip is not None:
            try:
                self._opt_tip.destroy()
            except Exception:
                pass
            self._opt_tip = None

    def _blank_all(self, fg, except_keys=()):
        for col in self.columns:
            if col.key == "symbol" or col.key in except_keys:
                continue
            if col.kind == "marker":
                _c, circles, pause, _i = self.markers[col.key]
                pause.config(text="")
                for _skey, c in circles:
                    c.config(text="●", fg=fg)
                continue
            self.labels[col.key].config(text="—", fg=fg)

    def _render_marker(self, col, ds, trained, no_trade, bg):
        """A jelölő-oszlop köreinek frissítése egy STRATÉGIÁHOZ (col.strategy_name):
        stádiumonként egy kör a strategy_cells[strat][stádium] cellából (glifa+szín).
        Ha a stratégia ezen az instrumentumon KI van kapcsolva → halvány pontok.
        A no-trade órát KÜLÖN ⏸ jel mutatja a doboz elején — nem az első kört
        cseréli le, így az irány-szín (zöld/piros pötty) a szünet alatt is látszik."""
        _frame, circles, pause, _inner = self.markers[col.key]
        sname = col.strategy_name
        enabled_list = getattr(ds, "enabled_strategies", None) or []
        # Üres lista → az egyetlen/aktív stratégia engedélyezett (visszafelé komp.).
        strat_enabled = (sname in enabled_list) if enabled_list else True
        cells = ds.strategy_cells.get(sname, {}) if (trained and strat_enabled) else {}
        pause.config(text="⏸" if (no_trade and strat_enabled) else "", bg=bg)
        for skey, c in circles:
            if not strat_enabled:
                # Kikapcsolt stratégia ezen az instrumentumon: apró pont (nem kör)
                # → ránézésre elválik, melyik stratégia él az adott soron.
                c.config(text="·", fg=FG_GRAY_DIM, bg=bg)
                continue
            cell = cells.get(skey)
            if cell:
                c.config(text=cell[0], fg=sem_color(cell[1]), bg=bg)
            else:
                c.config(text="●", fg=FG_GRAY, bg=bg)

    def _render_tfalign(self, ds, bg):
        """TF-együttállás cella: idősíkonként egy színes pont (zöld=fölfelé /
        piros=lefelé / szürke=semleges) + egy erős „S", ha MIND egy irányba mutat
        (zöld BUY / piros SELL). A pontokat az idősíkok számához igazítva építjük."""
        if not self.tfalign:
            return
        signs = getattr(ds, "tf_align_signs", None) or []
        direction = getattr(ds, "tf_align_dir", None)
        inner, dots = self.tfalign["inner"], self.tfalign["dots"]
        # Pontok (újra)építése, ha a szám változott (config-váltás/első frissítés).
        if len(dots) != len(signs):
            _tclick = (lambda e: self._on_tfalign(self.symbol)) if self._on_tfalign else None
            for w in inner.winfo_children():
                w.destroy()
            dots = []
            for _ in signs:
                d = tk.Label(inner, text="●", bg=bg, fg=FG_GRAY, font=self._mono, padx=1)
                if _tclick is not None:
                    d.config(cursor="hand2")
                    d.bind("<Button-1>", _tclick)
                d.pack(side="left")
                dots.append(d)
            s_lbl = tk.Label(inner, text=" ", bg=bg, fg=FG_GRAY, font=self._mono, padx=1)
            if _tclick is not None:
                s_lbl.config(cursor="hand2")
                s_lbl.bind("<Button-1>", _tclick)
            s_lbl.pack(side="left")
            self.tfalign["dots"], self.tfalign["s"] = dots, s_lbl
        # Pontok színe a per-idősík irányból.
        for d, s in zip(self.tfalign["dots"], signs):
            col = FG_GREEN if s > 0 else FG_RED if s < 0 else FG_GRAY
            d.config(fg=col, bg=bg)
        # Erős „S" csak együttállásnál (különben halvány „–").
        s_lbl = self.tfalign["s"]
        if s_lbl is not None:
            if direction == "BUY":
                s_lbl.config(text="S", fg=FG_GREEN, bg=bg)
            elif direction == "SELL":
                s_lbl.config(text="S", fg=FG_RED, bg=bg)
            else:
                s_lbl.config(text="–", fg=FG_GRAY_DIM, bg=bg)

    def update(self, ds, inst_state: str, opt_status: str, connected: bool = True,
               no_trade: bool = False):
        trained      = ds.trained
        has_position = ds.position_pnl is not None
        self._opt_full = opt_status or ""     # a tooltip a belépéskori teljes szöveget mutatja

        if inst_state == "OPTIMIZING":
            bg = BG_OPT_ROW
        elif not trained:
            bg = BG_UNTRAINED
        elif inst_state == "STOPPED":
            bg = BG_INACTIVE
        elif no_trade:
            bg = BG_INACTIVE   # LIVE, de no-trade óra (aktív stratégia) → "letiltva"
        else:
            bg = self._bg
        self.frame.config(bg=bg)
        self.ctrl_frame.config(bg=bg)
        for lbl in self.labels.values():
            lbl.config(bg=bg)
        for frame, circles, pause, inner in self.markers.values():
            frame.config(bg=bg)
            inner.config(bg=bg)
            pause.config(bg=bg)
            for _skey, c in circles:
                c.config(bg=bg)

        sym_lbl = self.labels["symbol"]

        # „R" gomb = kockázatcsökkentő PRESET (kattintásra körbe-vált). A gomb a
        # ténylegesen érvényes presetet mutatja: — Ki | R Risky | F Felező | P Pajzs
        # | Fi Fibo.
        _rp = getattr(ds, "rr_preset", "off")
        _rrmap = {"risky": ("R", FG_ORANGE), "halving": ("F", FG_CYAN),
                  "shield": ("P", FG_GREEN), "fibo": ("Fi", FG_YELLOW),
                  "thirds": ("H", FG_PURPLE), "shield_fibo": ("PF", FG_TEAL)}
        if _rp in _rrmap:
            _txt, _col = _rrmap[_rp]
            self.btn_risky.config(text=_txt, bg=_col, fg="#1e1e2e", state="normal")
        else:
            self.btn_risky.config(text="—", bg=BTN_DIS_BG, fg=FG_GRAY, state="normal")

        # Viz gomb — bármely állapotban kapcsolható; zöld, ha a viz BE van kapcsolva
        if getattr(ds, "viz_enabled", True):
            self.btn_viz.config(text="V", bg=FG_GREEN, fg="#1e1e2e", state="normal")
        else:
            self.btn_viz.config(text="V", bg=BTN_DIS_BG, fg=FG_GRAY, state="normal")

        # Jel-replay gomb — zöld, ha a belépő-jelzés vonalak (jel-replay) látszanak
        if getattr(ds, "show_trades", True):
            self.btn_trades.config(text="K", bg=FG_GREEN, fg="#1e1e2e", state="normal")
        else:
            self.btn_trades.config(text="K", bg=BTN_DIS_BG, fg=FG_GRAY, state="normal")

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
            # QUEUED → STOP (sorból törlés); OPTIMIZING (fut) → STOP (leállítás-
            # kérés: a szubprocessz trial-/lépés-határon áll le, eredmény eldobva).
            self._morph_btn(self.btn_opt, "STOP", True, BTN_STOP_BG, BTN_STOP_FG)
            self._morph_btn(self.btn_del, "✕", False, BG_INACTIVE, FG_RED)
            return

        # ── LIVE / STOPPED ──────────────────────────────────────────────────
        if inst_state == "LIVE":
            if no_trade:
                # Aktív, de az aktuális (bróker-)óra a stratégia trade_hours-ából
                # kimarad → "letiltott" kinézet (mint egy disabled gomb) + ⏸ jel.
                sym_lbl.config(text=f"⏸ {self.symbol}", fg=FG_GRAY,
                               font=("Courier", 9, "italic"))
            else:
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
            elif col.kind == "marker":
                self._render_marker(col, ds, trained, no_trade, bg)
            elif col.kind == "tfalign":
                self._render_tfalign(ds, bg)
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
        self._ctrl_hdr = None
        for i, col in enumerate(columns):
            # A Vezérlés fejléc az Opt státusz ELÉ kerül (a sorokban is a gombok
            # előzik meg a státuszt); az Opt státusz a maradék szélességet kapja.
            # A Vezérlés cella fix PIXEL-szélességű keret: a sorok gombsorának
            # tényleges szélességét a sync_ctrl_width() tükrözi rá (pontos igazítás).
            if col.key == "opt":
                self._ctrl_hdr = tk.Frame(self.frame, bg=BG_HEADER)
                _cl = tk.Label(self._ctrl_hdr, text="Vezérlés", anchor="w",
                               bg=BG_HEADER, fg=FG_BLUE, font=header_font,
                               padx=4, pady=3)
                _cl.pack(fill="both", expand=True)
                self._ctrl_hdr.config(width=_cl.winfo_reqwidth(),
                                      height=_cl.winfo_reqheight())
                self._ctrl_hdr.pack_propagate(False)
                self._ctrl_hdr.pack(side="left")
            lbl = tk.Label(
                self.frame, text=col.header, width=col.width, anchor=col.anchor,
                bg=BG_HEADER, fg=FG_BLUE, font=header_font,
                padx=4, pady=3, cursor="hand2",
            )
            if on_col_click:
                lbl.bind("<Button-1>", lambda e, idx=i: on_col_click(idx))
            lbl.pack(side="left", fill="x" if col.key == "opt" else "none",
                     expand=(col.key == "opt"))
            self._lbls.append(lbl)
        tk.Frame(parent, bg=FG_GRAY_DIM, height=1).pack(fill="x", padx=2)

    def sync_ctrl_width(self, px: int):
        """A Vezérlés fejléc-cella szélessége = a sorok gombsorának TÉNYLEGES
        pixel-szélessége (a PairRow ctrl_frame <Configure>-je hívja)."""
        if self._ctrl_hdr is not None and px > 1:
            self._ctrl_hdr.config(width=px)

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
        # Utolsó haladás időbélyege páronként — a "nem halad" (stall) timeouthoz
        self._last_progress: dict = {}

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
        """A gyermekfolyamatok haladását a fő státusz dict-be vezeti. A progress-queue
        szimbólum-szintű (symbol, done, total); a MELYIK stratégia a futó tételből
        derül ki (egy szimbólumon egyszerre egy fut)."""
        while True:
            try:
                symbol, done, total = self._progress_q.get()
            except Exception:
                break
            strat = next((st for (s, st) in self._running if s == symbol), None)
            if strat is not None:
                pct = int(done / total * 100) if total else 0
                self.optimizer_status[symbol] = f"{strat} {done}/{total}  {pct}%"
                self._last_progress[symbol] = time.time()   # halad → stall-óra újraindul

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
    # A munkatételek (symbol, strategy) párok — így per-stratégia tudjuk, MELYIK
    # optimalizál. A KIJELZÉS (instrument_state / optimizer_status) szimbólum-szintű
    # marad (aggregátum): egy szimbólumon EGYSZERRE EGY stratégia optimalizál (a
    # másik sorba kerül), így a szimbólum egyértelműen egy futó tételt azonosít.

    def _default_strategy(self) -> str:
        return getattr(self.strategy, "name", "wpr_sma")

    def _symbol_busy(self, symbol: str) -> bool:
        """Fut vagy sorban áll-e MÁR bármely stratégia ezen a szimbólumon."""
        return (any(s == symbol for s, _ in self._running)
                or any(s == symbol for s, _ in self._queue))

    def request_optimize(self, symbol: str, strategy: str | None = None):
        """Egy (symbol, strategy) optimalizálás kérése. Ugyanarra a szimbólumra TÖBB
        stratégia is kérhető — egyszerre EGY fut, a többi sorba kerül. LIVE
        (kereskedő) szimbólumot nem optimalizálunk."""
        strategy = strategy or self._default_strategy()
        with self._lock:
            if self.instrument_state.get(symbol) == "LIVE":
                return
            job = (symbol, strategy)
            if job in self._running or job in self._queue:
                return                       # ezt a stratégiát már kérték
            # Elavult leállítás-marker törlése: MOST kértek friss futást — egy
            # korábbi (már lezárt futás után maradt) STOP ne szakítsa meg azonnal.
            try:
                from core.params_store import stop_marker
                stop_marker(symbol, strategy).unlink(missing_ok=True)
            except Exception:
                pass
            symbol_running = any(s == symbol for s, _ in self._running)
            if len(self._running) < self.max_parallel and not symbol_running:
                self._start(job)
            else:
                self._queue.append(job)
                # Ha a szimbólum MÁR optimalizál (más stratégia), maradjon OPTIMIZING;
                # különben QUEUED (a pool tele van).
                if not symbol_running:
                    self.instrument_state[symbol] = "QUEUED"
                    self.optimizer_status[symbol] = f"Várakozik... ({strategy})"

    def cancel_queued(self, symbol: str):
        """Sorban álló (QUEUED) optimalizálás visszavonása (a szimbólum összes
        sorban álló tétele)."""
        with self._lock:
            self._queue = [(s, st) for (s, st) in self._queue if s != symbol]
            if not self._symbol_busy(symbol):
                self.instrument_state[symbol] = "STOPPED"
                self.optimizer_status[symbol] = ""

    def request_stop(self, symbol: str):
        """FUTÓ optimalizálás/tanítás leállítás-kérése + a szimbólum sorban álló
        tételeinek törlése. A stop-marker fájlt a szubprocessz trial-/lépés-
        határon észleli (optuna: study.stop) → az eredmény ELDOBVA, a korábban
        mentett paraméterek érintetlenek, nincs auto-folytatás."""
        from core.params_store import stop_marker
        with self._lock:
            running = [j for j in self._running if j[0] == symbol]
        for s, strat in running:
            try:
                stop_marker(s, strat).touch()
            except Exception:
                pass
        self.cancel_queued(symbol)          # a sorban állók is törölve
        if running:
            self.optimizer_status[symbol] = "Leállítás kérve..."

    def resume_unfinished(self):
        """INDÍTÁSKOR: a fájlrendszerben talált BEFEJEZETLEN study-k (van `_study.db`,
        nincs `.done`) automatikus sorba állítása — per (symbol, strategy). Az optuna a
        `.db`-ből folytat. A LIVE (kereskedő) szimbólumokat kihagyja (azok szándéka a
        kereskedés). A `live_trader` induló állapot-beállítása után hívandó (kis
        késleltetéssel, hogy a LIVE jelölés már beálljon)."""
        try:
            from core.params_store import unfinished_studies
            pending = unfinished_studies()
        except Exception:
            return
        for symbol, strat in pending:
            if self.instrument_state.get(symbol) == "LIVE":
                continue                     # kereskedő pár — nem optimalizáljuk
            self.request_optimize(symbol, strat)   # per-job dedup + sor a request-ben

    def _start(self, job):
        symbol, strategy = job
        self._running.add(job)
        self.instrument_state[symbol] = "OPTIMIZING"
        self.optimizer_status[symbol] = f"Indul... ({strategy})"
        threading.Thread(target=self._run_worker, args=(job,), daemon=True).start()

    def _run_worker(self, job):
        """HáttérSZÁL: adat-előkészítés (MT5, IO) → a CPU-nehéz optimalizálás
        külön PROCESSZBE. A fő (UI) szál egyiket sem érinti → nem fagy.
        `job` = (symbol, strategy) — per-stratégia optimalizálás."""
        symbol, strategy = job
        try:
            from ml.optimizer import optimize_job, params_file
            from trading.backtest import load_data
            from strategy import get_strategy_by_name
            job_strat = get_strategy_by_name(strategy)

            opt_cfg     = self.cfg["optimizer"]
            initial_bal = self.cfg.get("ml", {}).get("starting_balance_eur", 1000.0)

            # ── Adat előkészítés (háttérszálon) ───────────────────────────
            from core import mt5_connector as _mt5c
            from tools.download_history import download_pair, _fill_gap
            from datetime import datetime as _dt, timezone as _tz

            end_dt = _dt.now(_tz.utc)
            # MINDENKI ugyanazt a connect()-et használja → egységes terminál
            # (config mt5.path) + fiók-ellenőrzés. (A connect() maga foglalja a
            # MT5_LOCK-ot, ezért NEM tesszük külön lock-blokkba — az deadlock lenne.)
            connected = _mt5c.connect(self.cfg)

            if connected:
                for tf in (t.label for t in job_strat.timeframes()):
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

            # ── KÖZÖS dispatch: az optimize_job (→ optimize_symbol) dönt a
            #    módszerről (optuna|grid|random), szeletel, CSV-t ír és tesztel —
            #    PONTOSAN ugyanaz, mint a CLI-ben. A GUI csak a processzt/timeoutot/
            #    haladást intézi. A method-választás EGY helyen (optimize_symbol) él. ──
            self._ensure_pool()
            # "Nem halad" (stall) alapú védelem: NEM a teljes futásidőt limitáljuk
            # (ezek hosszú folyamatok!), hanem azt figyeljük, hogy jön-e haladás.
            # Ha stall_timeout_sec ideje NINCS előrelépés → tényleg beragadt → zárjuk.
            # hard_timeout_sec (0 = kikapcsolva) opcionális abszolút végső határ.
            stall_sec = opt_cfg.get("stall_timeout_sec", 900)   # 15 perc haladás nélkül
            hard_cap  = opt_cfg.get("hard_timeout_sec", 0)      # 0 = nincs abszolút limit
            self.optimizer_status[symbol] = "Optimalizálás indul..."
            args = (symbol, df_m15, df_m1, self.cfg, initial_bal)

            if self._pool is not None:
                from concurrent.futures import TimeoutError as _FutTimeout
                t_submit = time.time()
                self._last_progress[symbol] = t_submit
                fut = self._pool.submit(optimize_job, *args, self._progress_q, strategy)
                while True:
                    try:
                        entry = fut.result(timeout=10)   # rövid poll
                        break
                    except _FutTimeout:
                        now  = time.time()
                        idle = now - self._last_progress.get(symbol, now)
                        if idle > stall_sec:
                            fut.cancel()
                            self._log_error(symbol,
                                f"BERAGADT: {int(idle)} mp nincs haladás "
                                f"(stall_timeout_sec={stall_sec}). Lehet lassú (nagy adat) "
                                f"vagy tényleg elakadt. Nézd meg a trials CSV-t, ha létrejött.")
                            self.optimizer_status[symbol] = \
                                f"Hiba: beragadt ({int(idle//60)} perc nincs haladás)"
                            return   # a finally STOPPED-ra állít → UI nem ragad be
                        if hard_cap and (now - t_submit) > hard_cap:
                            fut.cancel()
                            self._log_error(symbol, f"ABSZOLÚT IDŐLIMIT ({hard_cap} mp) elérve.")
                            self.optimizer_status[symbol] = "Hiba: abszolút időlimit"
                            return
            else:
                entry = optimize_job(*args, _LocalProgress(self.optimizer_status), strategy)

            if "error" in entry:
                if entry.get("stopped"):
                    # User-cancel (STOP gomb) — nem hiba: rövid státusz, nincs log.
                    self.optimizer_status[symbol] = "Megszakítva ✋"
                    return
                self.optimizer_status[symbol] = f"Hiba: {entry['error']}"
                self._log_error(
                    symbol, entry.get("traceback") or f"eredmény hiba: {entry['error']}")
                return

            full = {
                "symbol":       symbol,
                "optimized_at": datetime.utcnow().isoformat(),
                **entry,
            }
            # rr-optimalizálás eredménye (ha volt): tisztán ne írjunk "rr": null-t;
            # ha van, a JSON-ba kerül ÉS a live per-pár állapotba (rr_state).
            _rr = full.get("rr")
            if not _rr:
                full.pop("rr", None)
            out = params_file(symbol, strategy)
            tmp = out.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(full, f, indent=2, ensure_ascii=False, default=str)
            tmp.replace(out)
            if _rr:
                from ml.optimizer import apply_optimized_rr
                apply_optimized_rr(symbol, _rr)

            # Sikeres: a pár azonnal "tanított" → Play aktiválható
            ds = self.dashboard_ref.get(symbol)
            if ds is not None:
                ds.trained = True
            # A frissen írt done-marker idejéből a perzisztens 'Utolsó opt: <dátum>'
            # címke — a MOST futtatott stratégia markeréből (pl. ml_ai tanítás).
            self.optimizer_status[symbol] = opt_done_label(symbol, strategy) or "Kész ✓"

        except Exception as e:
            import traceback
            self._log_error(symbol, traceback.format_exc())
            self.optimizer_status[symbol] = f"Hiba: {e}"
        finally:
            with self._lock:
                self._running.discard(job)
                # A szimbólum csak akkor lesz STOPPED, ha NINCS több futó/sorban álló
                # tétele (más stratégia még optimalizálhat ugyanarra a szimbólumra).
                if not self._symbol_busy(symbol):
                    self._last_progress.pop(symbol, None)
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
        # A sorból az ELSŐ olyan tételt indítjuk, amelynek a szimbóluma épp NEM fut
        # (egy szimbólumon egyszerre egy stratégia optimalizál). A többi a sorban marad.
        while len(self._running) < self.max_parallel:
            nxt = next((j for j in self._queue
                        if not any(s == j[0] for s, _ in self._running)), None)
            if nxt is None:
                break
            self._queue.remove(nxt)
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

        # ── Stratégia-választó — a portfólió ezen a stratégián fut; a párlista is
        # ehhez igazodik (az adott stratégia optimalizált almappája). ──────────
        from strategy import available_strategy_names, default_strategy_name
        strat_row = tk.Frame(ctrl, bg=BG_BT)
        strat_row.pack(fill="x", pady=(0, 4))
        tk.Label(strat_row, text="Stratégia:", bg=BG_BT, fg=FG_BLUE,
                 font=self._header).pack(side="left")
        self._strat_var = tk.StringVar(value=default_strategy_name(self.cfg))
        _snames = available_strategy_names(self.cfg)
        self._strat_menu = tk.OptionMenu(strat_row, self._strat_var, *_snames,
                                         command=lambda _=None: self._reload_symbols())
        self._strat_menu.config(bg=BG_HEADER, fg=FG_WHITE, font=self._small,
                                relief="flat", highlightthickness=0,
                                activebackground=BG_HEADER)
        self._strat_menu["menu"].config(bg=BG_HEADER, fg=FG_WHITE)
        self._strat_menu.pack(side="left", padx=6)

        tk.Label(ctrl, text="Instrumentumok (optimalizáltak):",
                 bg=BG_BT, fg=FG_BLUE, font=self._header).pack(anchor="w", pady=(2, 4))
        # A párlista dinamikusan újraépül a stratégiaváltásra.
        self._sym_frame = tk.Frame(ctrl, bg=BG_BT)
        self._sym_frame.pack(fill="x")
        self._sym_vars: dict = {}
        self._reload_symbols()

        # ── Űrlap: dátum / tőke / slotok / kockázatcsökkentés / építés / gombok ─
        form = tk.Frame(ctrl, bg=BG_BT)
        form.pack(fill="x", pady=(8, 0))

        tk.Label(form, text="Tól:", bg=BG_BT, fg=FG_GRAY,
                 font=self._small).grid(row=0, column=0, sticky="e", pady=6)
        self._entry_from = tk.Entry(form, width=12, bg=BG_HEADER, fg=FG_WHITE,
                                    font=self._small, insertbackground=FG_WHITE)
        self._entry_from.insert(0, self.cfg.get("optimizer", {}).get(
            "test_start_date", "2025-10-01"))
        self._entry_from.grid(row=0, column=1, padx=4)

        tk.Label(form, text="Ig:", bg=BG_BT, fg=FG_GRAY,
                 font=self._small).grid(row=0, column=2, sticky="e")
        self._entry_to = tk.Entry(form, width=12, bg=BG_HEADER, fg=FG_WHITE,
                                  font=self._small, insertbackground=FG_WHITE)
        self._entry_to.insert(0, datetime.now().strftime("%Y-%m-%d"))
        self._entry_to.grid(row=0, column=3, padx=4)

        tk.Label(form, text="Kezdő tőke ($):", bg=BG_BT, fg=FG_GRAY,
                 font=self._small).grid(row=1, column=0, sticky="e", pady=4)
        self._entry_bal = tk.Entry(form, width=10, bg=BG_HEADER, fg=FG_WHITE,
                                   font=self._small, insertbackground=FG_WHITE)
        self._entry_bal.insert(0, str(int(
            self.cfg.get("ml", {}).get("starting_balance_eur", 1000))))
        self._entry_bal.grid(row=1, column=1, padx=4)

        # Egyszerre nyitott (nem risk-free) pozíciók száma — alap: trading.max_open_slots.
        tk.Label(form, text="Slotok:", bg=BG_BT, fg=FG_GRAY,
                 font=self._small).grid(row=1, column=2, sticky="e", pady=4)
        self._entry_slots = tk.Entry(form, width=6, bg=BG_HEADER, fg=FG_WHITE,
                                     font=self._small, insertbackground=FG_WHITE)
        self._entry_slots.insert(0, str(int(
            self.cfg.get("trading", {}).get("max_open_slots", 4))))
        self._entry_slots.grid(row=1, column=3, padx=4, sticky="w")

        # Kockázatcsökkentés preset (MIND a párra) — a technikák összevetéséhez.
        tk.Label(form, text="Kockázatcsökkentés:", bg=BG_BT, fg=FG_GRAY,
                 font=self._small).grid(row=2, column=0, sticky="e", pady=4)
        self._rr_var = tk.StringVar(value="Auto (jelenlegi)")
        self._rr_combo = ttk.Combobox(
            form, textvariable=self._rr_var, width=16, state="readonly",
            font=self._small,
            values=["Auto (jelenlegi)", "Ki (mind)", "Risky (mind)",
                    "Felező (mind)", "Pajzs (mind)", "Fibo (mind)",
                    "Harmados (mind)", "Pajzs↔Fibo (mind)"])
        self._rr_combo.grid(row=2, column=1, padx=4, sticky="w")

        # Pozícióépítés (piramidális ráépítés a risk-free runnereken) ki/be.
        self._build_var = tk.BooleanVar(value=False)
        tk.Checkbutton(form, text="Pozícióépítés", variable=self._build_var,
                       bg=BG_BT, fg=FG_WHITE, selectcolor=BG_HEADER,
                       activebackground=BG_BT, activeforeground=FG_WHITE,
                       font=self._small).grid(row=2, column=2, columnspan=2,
                                              sticky="w", padx=4)

        self._btn_start = tk.Button(form, text="▶  Backtest indítása", width=20,
                                    bg=BTN_BT_BG, fg=BTN_BT_FG, font=self._small,
                                    relief="flat", command=self._start_bt)
        self._btn_start.grid(row=3, column=0, columnspan=2, pady=8, sticky="w")

        self._btn_stop_bt = tk.Button(form, text="■  Leállítás", width=12,
                                      bg=BTN_DIS_BG, fg=BTN_DIS_FG, font=self._small,
                                      relief="flat", command=self._stop_bt,
                                      state="disabled")
        self._btn_stop_bt.grid(row=3, column=2, columnspan=2, pady=8, sticky="w")

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

    def _reload_symbols(self):
        """A párlista (jelölőnégyzetek) újraépítése a választott stratégia
        optimalizált almappájából (stratégiaváltáskor és induláskor)."""
        from core.params_store import strategy_dir
        for w in self._sym_frame.winfo_children():
            w.destroy()
        self._sym_vars = {}
        strat_name = getattr(self, "_strat_var", None)
        params_dir = strategy_dir(strat_name.get() if strat_name else None)
        # A *_hours.json a kereskedési-óra fájl, NEM optimalizált param — kiszűrjük
        # (ilyet kiválasztva a portfólió-backtest elszállna a hiányzó params miatt).
        optimized  = sorted([f.stem for f in params_dir.glob("*.json")
                             if not f.stem.endswith("_hours")]) \
                     if params_dir.exists() else []
        if not optimized:
            tk.Label(self._sym_frame, text="(Nincs optimalizált instrumentum)",
                     bg=BG_BT, fg=FG_GRAY, font=self._small).grid(
                         row=0, column=0, columnspan=4, sticky="w")
            return
        cols = 4
        for i, sym in enumerate(optimized):
            var = tk.BooleanVar(value=True)
            self._sym_vars[sym] = var
            tk.Checkbutton(self._sym_frame, text=sym, variable=var,
                           bg=BG_BT, fg=FG_WHITE, selectcolor=BG_HEADER,
                           activebackground=BG_BT, activeforeground=FG_WHITE,
                           font=self._small).grid(row=i // cols, column=i % cols,
                                                  sticky="w", padx=6)

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
        try:
            n_slots = max(1, int(self._entry_slots.get().strip()))
        except ValueError:
            n_slots = None          # → a config max_open_slots
        build_on   = self._build_var.get()
        strat_name = self._strat_var.get()

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
            args=(symbols, date_from, date_to, init_bal, self._rr_spec(),
                  strat_name, n_slots, build_on),
            daemon=True,
        )
        self._thread.start()
        self.parent.after(200, self._poll_progress)

    def _stop_bt(self):
        self._stop_flag.set()
        self._lbl_status.config(text="Leállítás...", fg=FG_ORANGE)

    def _rr_spec(self):
        """A választott preset → kockázatcsökkentő spec (mind a párra), vagy None
        ('Auto' = a per-pár auto-risky, a jelenlegi viselkedés)."""
        from core import risk_reduction as _rr
        preset = {"Ki (mind)": _rr.PRESET_OFF, "Risky (mind)": _rr.PRESET_RISKY,
                  "Felező (mind)": _rr.PRESET_HALVING,
                  "Pajzs (mind)": _rr.PRESET_SHIELD,
                  "Fibo (mind)": _rr.PRESET_FIBO,
                  "Harmados (mind)": _rr.PRESET_THIRDS,
                  "Pajzs↔Fibo (mind)": _rr.PRESET_SHIELD_FIBO}.get(self._rr_var.get())
        if preset is None:
            return None
        return {**_rr.default_config(), "preset": preset}

    def _run_thread(self, symbols, date_from, date_to, init_bal, rr_spec=None,
                    strat_name=None, n_slots=None, build_on=False):
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
                rr=rr_spec,
                strategy_name=strat_name,
                max_slots=n_slots,
                build=build_on,
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

            is_risky = s.get("risky", False)

            bg = BG_ROW_ODD if row_idx % 2 == 0 else BG_ROW_EVEN
            fr = tk.Frame(self._res_rows_frame, bg=bg)
            fr.pack(fill="x")
            vals = [
                (f"{sym} ⚠R" if is_risky else sym, 10, FG_ORANGE if is_risky else FG_WHITE),
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

        risky_pairs = result.get("risky_pairs", [])
        risky_note  = (f"   ·   ⚠R risky ({len(risky_pairs)}): {', '.join(risky_pairs)}"
                       if risky_pairs else "")
        self._lbl_res_total.config(
            text=f"ÖSSZESEN  |  Trade: {n_all}  |  Win: {wr_all:.0%}  |  "
                 f"P&L: {total_pnl:+.2f}$  |  MaxDD: {mdd:.1f}%  |  "
                 f"PF: {pf_str}  |  Végegyenleg: ${final_bal:.0f}{risky_note}",
            fg=FG_GREEN if total_pnl >= 0 else FG_RED,
        )


# ---------------------------------------------------------------------------
# Pozíciók fül — nyitott pozíciók kezelése
# ---------------------------------------------------------------------------

POSITION_COLUMNS = [
    ("symbol",  "Symbol",     10, "w"),
    ("strategy","Stratégia",   9, "center"),
    ("type",    "Irány",       6, "center"),
    ("volume",  "Lot",         6, "center"),
    ("open",    "Nyitó",      10, "center"),
    ("current", "Akt.",       10, "center"),
    ("dist",    "Belépő táv",  9, "center"),   # pont a belépőtől (profit-irányban)
    ("sl",      "SL",         10, "center"),
    ("sl_pnl",  "SL P&L",      9, "center"),   # lekötött eredmény, ha az SL bekövetkezik
    ("tp",      "TP",         10, "center"),
    ("orig_sl", "Er. SL",     10, "center"),
    ("pnl",     "P&L",         9, "center"),
    ("r_mult",  "R",           6, "center"),   # folyó R-szorzó: (ár−belépő)/|belépő−er.SL|
]


class PositionRow:
    def __init__(self, parent, ticket, mono_font, small_font,
                 on_be, on_trail, on_panic, on_name_click, on_trail_dist,
                 on_build=None, on_build_mode=None):
        self.ticket = ticket
        self._small = small_font
        self._symbol = None
        self._on_name_click = on_name_click
        self._on_trail_dist = on_trail_dist
        self._on_build_mode = on_build_mode
        self.frame = tk.Frame(parent, bg=BG_ROW_EVEN)
        self.labels = {}
        for key, hdr, w, anchor in POSITION_COLUMNS:
            lbl = tk.Label(self.frame, text="—", width=w, anchor=anchor,
                           bg=BG_ROW_EVEN, fg=FG_WHITE, font=mono_font, padx=4, pady=2)
            lbl.pack(side="left")
            self.labels[key] = lbl
        # Symbol cellára kattintva → optimalizált paraméterek (mint a Live fülön)
        self.labels["symbol"].config(cursor="hand2")
        self.labels["symbol"].bind(
            "<Button-1>",
            lambda e: self._symbol and self._on_name_click(self._symbol))
        self.btn_be = tk.Button(self.frame, text="BE", width=4, font=small_font,
                                relief="flat", bg=BTN_OPT_BG, fg="#ffffff",
                                command=lambda: on_be(ticket))
        self.btn_be.pack(side="left", padx=1)
        # A BE-gomb tiltva, ha a profit még nem fedezi a költséget → tooltip mondja meg
        self._be_tip_text = ""
        self._be_tip = None
        self.btn_be.bind("<Enter>", self._be_tip_show)
        self.btn_be.bind("<Leave>", self._be_tip_hide)
        # Építés MÓD-váltó (Ki/Kézi/Auto) — per SZIMBÓLUM (a soron állítható, nem kell
        # az instrumentum-ablak). Kattintásra körben vált; a szín jelzi az állapotot.
        self.btn_bmode = tk.Button(self.frame, text="Ép:—", width=7, font=small_font,
                                   relief="flat", bg=BTN_DIS_BG, fg=FG_GRAY,
                                   command=(lambda: self._symbol and on_build_mode(self._symbol))
                                           if on_build_mode else None)
        self.btn_bmode.pack(side="left", padx=1)
        # „＋" pozícióépítés (ráépítés): csak akkor aktív, ha az építés-mód Kézi és a
        # gyertyás jel szól (a pozíció kockázatmentes). Tooltip mondja meg az okot.
        self.btn_build = tk.Button(self.frame, text="＋", width=2, font=small_font,
                                   relief="flat", bg=BTN_DIS_BG, fg=FG_GRAY,
                                   state="disabled",
                                   command=(lambda: on_build(ticket)) if on_build else None)
        self.btn_build.pack(side="left", padx=1)
        self._build_tip_text = ""
        self._build_tip = None
        self.btn_build.bind("<Enter>", self._build_tip_show)
        self.btn_build.bind("<Leave>", self._build_tip_hide)
        self.btn_trail = tk.Button(self.frame, text="Trail", width=5, font=small_font,
                                   relief="flat", bg=BTN_DIS_BG, fg=FG_GRAY,
                                   command=lambda: on_trail(ticket))
        self.btn_trail.pack(side="left", padx=1)
        # Trail távolság (pip) — kézzel szerkeszthető; Enter/fókuszvesztés menti
        self._trail_var = tk.StringVar()
        self.ent_trail = tk.Entry(self.frame, textvariable=self._trail_var, width=4,
                                  font=small_font, bg=BG_HEADER, fg=FG_WHITE,
                                  insertbackground=FG_WHITE, relief="flat",
                                  justify="center")
        self.ent_trail.pack(side="left", padx=1)
        self.ent_trail.bind("<Return>",   self._apply_trail_dist)
        self.ent_trail.bind("<FocusOut>", self._apply_trail_dist)
        self.btn_panic = tk.Button(self.frame, text="Zár", width=4, font=small_font,
                                   relief="flat", bg=BTN_STOP_BG, fg="#ffffff",
                                   command=lambda: on_panic(ticket))
        self.btn_panic.pack(side="left", padx=(1, 4))

    def _apply_trail_dist(self, _event=None):
        # PONT, egész szám (tizedes nélkül). Toleráns beolvasás, de egészre kerekít.
        raw = self._trail_var.get().strip().replace(",", ".")
        try:
            val = int(round(float(raw)))
        except ValueError:
            return
        if val > 0:
            self._on_trail_dist(self.ticket, val)

    # ── BE-gomb tooltip (miért tiltott: a profit nem fedezi a költséget) ──────
    def _be_tip_show(self, _event=None):
        text = (self._be_tip_text or "").strip()
        if not text or self._be_tip is not None:
            return
        tip = tk.Toplevel(self.btn_be)
        tip.wm_overrideredirect(True)
        tip.attributes("-topmost", True)
        x = self.btn_be.winfo_rootx()
        y = self.btn_be.winfo_rooty() + self.btn_be.winfo_height() + 2
        tk.Label(tip, text=text, bg="#2a2a3a", fg="#e0e0f0", font=self._small,
                 padx=6, pady=3, relief="solid", bd=1, justify="left",
                 wraplength=320).pack()
        tip.wm_geometry(f"+{x}+{y}")
        self._be_tip = tip

    def _be_tip_hide(self, _event=None):
        if self._be_tip is not None:
            try:
                self._be_tip.destroy()
            except Exception:
                pass
            self._be_tip = None

    def _build_tip_show(self, _event=None):
        text = (self._build_tip_text or "").strip()
        if not text or self._build_tip is not None:
            return
        tip = tk.Toplevel(self.btn_build)
        tip.wm_overrideredirect(True)
        tip.attributes("-topmost", True)
        x = self.btn_build.winfo_rootx()
        y = self.btn_build.winfo_rooty() + self.btn_build.winfo_height() + 2
        tk.Label(tip, text=text, bg="#2a2a3a", fg="#e0e0f0", font=self._small,
                 padx=6, pady=3, relief="solid", bd=1, justify="left",
                 wraplength=320).pack()
        tip.wm_geometry(f"+{x}+{y}")
        self._build_tip = tip

    def _build_tip_hide(self, _event=None):
        if self._build_tip is not None:
            try:
                self._build_tip.destroy()
            except Exception:
                pass
            self._build_tip = None

    def update(self, pos, pstate, digits, trail_default=None, point=None,
               strategy_name="—"):
        self._symbol = pos["symbol"]
        self.labels["symbol"].config(text=pos["symbol"])
        self.labels["strategy"].config(text=strategy_name or "—", fg=FG_GRAY)
        t = pos["type"]
        self.labels["type"].config(text=t, fg=FG_GREEN if t == "BUY" else FG_RED)
        self.labels["volume"].config(text=f'{pos["volume"]:.2f}', fg=FG_WHITE)
        self.labels["open"].config(text=_fmt_price(pos["price_open"], digits), fg=FG_GRAY)
        self.labels["current"].config(text=_fmt_price(pos["price_current"], digits), fg=FG_WHITE)

        entry   = pos["price_open"]
        cur     = pos["price_current"]
        sl_lvl  = pos["sl"]
        profit  = pos["profit"]
        dir_s   = 1 if t == "BUY" else -1   # a profit iránya

        # Belépő táv PONTBAN, a profit irányában előjelezve (+ = javamra mozdult)
        if point and point > 0:
            dist_pts = int(round((cur - entry) / point * dir_s))
            self.labels["dist"].config(text=f"{dist_pts:+d}",
                                       fg=FG_GREEN if dist_pts >= 0 else FG_RED)
        else:
            self.labels["dist"].config(text="—", fg=FG_GRAY)

        # P&L, ha az AKTUÁLIS SL bekövetkezik. A profit lineáris az árban, így a
        # jelenlegi lebegő P&L-ből arányosítható: pnl_sl = P&L × (SL−entry)/(ár−entry).
        # Ahogy a trailing emeli az SL-t, ez az érték egyre nyereségesebb lesz.
        if sl_lvl and abs(cur - entry) > (point or 1e-9):
            sl_pnl = profit * (sl_lvl - entry) / (cur - entry)
            self.labels["sl_pnl"].config(text=f"{sl_pnl:+.2f}$",
                                         fg=FG_GREEN if sl_pnl >= 0 else FG_RED)
        else:
            self.labels["sl_pnl"].config(text="—", fg=FG_GRAY)

        sl, tp = pos["sl"], pos["tp"]
        orig = pstate.get("original_sl", sl) if pstate else sl
        be_done = bool(pstate and pstate.get("be_done"))
        trail_moved = bool(pstate and pstate.get("trail_moved"))
        moved = bool(sl and orig and abs(sl - orig) > 1e-9)
        # SL kijelzés: ha a TRAILING mozgatta → zöld + irányjel + "T" (látható, hogy
        # a trailing húzta); ha BE megvolt de a trailing még nem húzott → cián.
        if sl:
            sl_txt = _fmt_price(sl, digits)
            if trail_moved and moved:
                arrow = "⇗" if pos["type"] == "BUY" else "⇘"
                self.labels["sl"].config(text=f"{sl_txt} {arrow}T", fg=FG_GREEN)
            else:
                self.labels["sl"].config(text=sl_txt,
                                         fg=FG_CYAN if be_done else FG_WHITE)
        else:
            self.labels["sl"].config(text="—", fg=FG_WHITE)
        self.labels["tp"].config(text=_fmt_price(tp, digits) if tp else "—", fg=FG_GRAY)
        # Eredeti SL: fehér, de ha a trailing már elmozdította → szürke
        self.labels["orig_sl"].config(text=_fmt_price(orig, digits) if orig else "—",
                                      fg=FG_GRAY if moved else FG_WHITE)
        pnl = pos["profit"]
        self.labels["pnl"].config(text=f"{pnl:+.2f}$", fg=FG_GREEN if pnl >= 0 else FG_RED)
        # Folyó R-szorzó: R = |belépő − EREDETI SL| (a kezdeti kockázat árban); a jelen
        # állás = (ár − belépő)/R a profit irányában. Egy mércén látod, „hány R-nél"
        # tartasz (a kockázatcsökkentés is 1R-nél lép). — üres, ha nincs eredeti SL.
        r_price = abs(entry - orig) if orig else 0.0
        if r_price > (point or 1e-9):
            r_mult = (cur - entry) / r_price * dir_s
            self.labels["r_mult"].config(text=f"{r_mult:+.2f}R",
                                         fg=FG_GREEN if r_mult >= 0 else FG_RED)
        else:
            self.labels["r_mult"].config(text="—", fg=FG_GRAY)

        # Gombok állapota (aktív-e?). A kézi BE csak akkor engedélyezett, ha a
        # költség-tudatos BE MOST mozgatható (a profit fedezi a spread+jutalék+swap
        # költséget) — különben TILTVA + tooltip, hogy ne lehessen némán nyomkodni.
        be_feasible = pos.get("be_feasible", True)   # True fallback (demo/régi cache)
        if be_done:
            self.btn_be.config(text="BE ✓", bg=BTN_PLAY_BG, fg="#ffffff", state="normal")
            self._be_tip_text = ""
        elif not be_feasible:
            self.btn_be.config(text="BE", bg=BTN_DIS_BG, fg=FG_GRAY, state="disabled")
            self._be_tip_text = ("BE még nem lehetséges — a nyereség nem fedezi a "
                                 "spread + jutalék + swap költséget.")
        else:
            self.btn_be.config(text="BE", bg=BTN_OPT_BG, fg="#ffffff", state="normal")
            self._be_tip_text = ""

        # Építés MÓD + „＋" gomb — a motor build_runtime-jából (per szimbólum). A mód
        # a soron állítható (Ép-gomb); a „＋" csak Kézi módban + a gyertyás jelre aktív.
        sym = pos.get("symbol")
        _rt = None
        try:
            from trading.live_trader import build_runtime as _br
            _rt = _br.get(sym) if sym else None
        except Exception:
            _rt = None
        # A tényleges mód: a build_runtime-ból, vagy közvetlenül a build_state-ből
        # (ha a motor még nem töltötte fel — pl. épp most állítottad át).
        _mode = (_rt or {}).get("mode")
        if _mode is None:
            try:
                from core import build_state as _bst
                _mode = _bst.get_mode(sym) if sym else "off"
            except Exception:
                _mode = "off"
        _MODE_LBL = {"off": "Ép:Ki", "manual": "Ép:Kézi", "auto": "Ép:Auto"}
        _MODE_COL = {"off": FG_GRAY, "manual": "#ffffff", "auto": FG_CYAN}
        self.btn_bmode.config(text=_MODE_LBL.get(_mode, "Ép:Ki"),
                              fg=_MODE_COL.get(_mode, FG_GRAY),
                              relief="sunken" if _mode in ("manual", "auto") else "flat")
        if _mode == "manual" and _rt and _rt.get("ready"):
            self.btn_build.config(state="normal", bg=BTN_OPT_BG, fg="#ffffff")
            self._build_tip_text = (f"Ráépítés: +{_rt.get('next_lot', 0):.2f} lot azonos "
                                    f"irányba, az összes stop az átlagárra "
                                    f"(≈{_rt.get('avg_price', 0):.5f}).")
        elif _mode == "manual":
            self.btn_build.config(state="disabled", bg=BTN_DIS_BG, fg=FG_GRAY)
            self._build_tip_text = ("Építés (Kézi): a +gomb akkor aktív, ha a pozíció "
                                    "kockázatmentes ÉS a gyertya új csúcsra/mélyre zár. "
                                    "Ekkor a +gomb hozzáad még egy (csökkenő méretű) "
                                    "pozíciót, és minden stopot az átlagárra húz.")
        elif _mode == "auto":
            # Auto: a motor magától épít → a gomb tiltva, de cián jelzi, hogy aktív.
            self.btn_build.config(text="＋", state="disabled", bg=BTN_DIS_BG, fg=FG_CYAN)
            self._build_tip_text = "Építés: Auto — a motor magától ráépít a jel-gyertyán."
        else:
            self.btn_build.config(state="disabled", bg=BTN_DIS_BG, fg=FG_GRAY)
            self._build_tip_text = ("Építés (pozícióépítés) kikapcsolva. Az Ép-gombbal "
                                    "kapcsold Kézi vagy Auto módba. Kézinél a +gomb "
                                    "hozzáad még egy pozíciót a jel-gyertyán.")

        # Trail gomb — 3 állapot, "benyomott" (sunken) ha be van kapcsolva:
        #   • KI:            lapos, szürke
        #   • BE, de VÁR:    benyomott, NARANCS (nincs még BE/kockázatmentes → nem húz)
        #   • BE és AKTÍV:   benyomott, ZÖLD (BE megvolt → a trailing húzhat/húz)
        trail_on = bool(pstate.get("trailing_enabled", True)) if pstate else True
        if not trail_on:
            self.btn_trail.config(text="Trail", relief="flat",
                                  bg=BTN_DIS_BG, fg=FG_GRAY)
        elif be_done:
            self.btn_trail.config(text="Trail", relief="sunken",
                                  bg=FG_GREEN, fg="#1e1e2e")
        else:
            self.btn_trail.config(text="Trail", relief="sunken",
                                  bg=FG_ORANGE, fg="#1e1e2e")

        # Trail távolság mező — PONTBAN, egész szám. A kézi felülírás, ha van;
        # egyébként az optimalizált alapérték. Gépelés közben NEM írjuk felül.
        override = pstate.get("trail_points") if pstate else None
        eff = override if override is not None else trail_default
        try:
            focused = self.ent_trail.focus_get() is self.ent_trail
        except Exception:
            focused = False
        if not focused:
            self._trail_var.set(str(int(eff)) if eff is not None else "")
        # Vizuális jelzés: kézi felülírás = cián, alapérték = halványabb
        self.ent_trail.config(fg=FG_CYAN if override is not None else FG_GRAY)


class PositionsTab:
    def __init__(self, parent, cfg, mono_font, small_font, header_font,
                 positions_provider, pos_state, digits_provider,
                 on_be, on_trail, on_panic, on_close_all,
                 on_name_click, on_trail_dist, trail_default_provider,
                 point_provider, strategy_provider=None, on_build=None,
                 on_build_mode=None):
        self.parent = parent
        self.cfg = cfg
        self._mono, self._small, self._header = mono_font, small_font, header_font
        self._positions_provider = positions_provider
        self._strategy_provider = strategy_provider
        self._pos_state = pos_state
        self._digits_provider = digits_provider
        self._on_be, self._on_trail, self._on_panic = on_be, on_trail, on_panic
        self._on_close_all = on_close_all
        self._on_name_click = on_name_click
        self._on_trail_dist = on_trail_dist
        self._on_build = on_build
        self._on_build_mode = on_build_mode
        self._trail_default_provider = trail_default_provider
        self._point_provider = point_provider
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

        # Jelmagyarázat — a Trail gomb színei és az SL trailing-jelölés
        legend = tk.Frame(p, bg=BG)
        legend.pack(fill="x", padx=10, pady=(0, 4))
        tk.Label(legend, text="Trail:", bg=BG, fg=FG_GRAY, font=self._small).pack(side="left")
        for txt, col in [("■ aktív", FG_GREEN), ("■ vár (nincs BE)", FG_ORANGE),
                         ("■ kikapcsolva", FG_GRAY)]:
            tk.Label(legend, text=txt, bg=BG, fg=col, font=self._small, padx=4).pack(side="left")
        tk.Label(legend, text="   SL ⇘T = trailing mozgatta   |   Belépő táv = pont a belépőtől "
                              "(+ = javamra)   |   SL P&L = eredmény, ha az SL bekövetkezik",
                 bg=BG, fg=FG_GRAY, font=self._small).pack(side="left")

        # Fejléc
        hdr = tk.Frame(p, bg=BG_HEADER)
        hdr.pack(fill="x", padx=2)
        for key, label, w, anchor in POSITION_COLUMNS:
            tk.Label(hdr, text=label, width=w, anchor=anchor, bg=BG_HEADER,
                     fg=FG_BLUE, font=self._header, padx=4, pady=3).pack(side="left")
        tk.Label(hdr, text="Vezérlés (BE / Ép:mód / ＋ / Trail / táv=pont / Zár)", width=40, anchor="w",
                 bg=BG_HEADER, fg=FG_BLUE, font=self._header).pack(side="left")
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
                                  self._on_be, self._on_trail, self._on_panic,
                                  self._on_name_click, self._on_trail_dist,
                                  on_build=self._on_build,
                                  on_build_mode=self._on_build_mode)
                self._rows[tid] = row
            trail_def = self._trail_default_provider(pos["symbol"])
            point     = self._point_provider(pos["symbol"])
            strat     = self._strategy_provider(pos.get("magic")) if self._strategy_provider else "—"
            row.update(pos, self._pos_state.get(tid),
                       self._digits_provider(pos["symbol"]), trail_def, point,
                       strategy_name=strat)

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
# Lezárt napi pozíciók fül — a mai (UTC) lezárt kereskedések + összesítés
# ---------------------------------------------------------------------------

CLOSED_COLUMNS = [
    ("symbol",   "Symbol",     10, "w"),
    ("strategy", "Stratégia",   9, "center"),
    ("type",     "Irány",       6, "center"),
    ("volume",   "Lot",         6, "center"),
    ("open",     "Nyitó",      10, "center"),
    ("close",    "Záró",       10, "center"),
    ("time",     "Zárás",       8, "center"),
    ("pnl",      "P&L",         9, "center"),
    ("r",        "R",           6, "center"),
]


def _r_multiple(c: dict):
    """A trade R-szorzója (ár-alapú): kedvező ármozgás / kezdeti SL-táv. Lot- és
    pip-érték-független, a klasszikus „R multiple". None, ha nincs érvényes SL."""
    sl = c.get("sl")
    po = c.get("price_open")
    if not sl or not po:
        return None
    risk = abs(po - sl)
    if risk <= 0:
        return None
    move = (c["price_close"] - po) if c["type"] == "BUY" else (po - c["price_close"])
    return move / risk


class ClosedTab:
    """Mai lezárt kereskedések (MT5 history), stratégiánkénti bontással.
    A sorok kulcsa a pozíció-azonosító; a lista a nap során csak bővül."""

    def __init__(self, parent, mono_font, small_font, header_font,
                 closed_provider, strategy_provider, digits_provider):
        self.parent = parent
        self._mono, self._small, self._header = mono_font, small_font, header_font
        self._closed_provider = closed_provider
        self._strategy_provider = strategy_provider
        self._digits_provider = digits_provider
        self._rows: dict = {}
        self._day = None          # az utolsó frissítés napja (napváltás-detektálás)
        self._build_ui()

    def _build_ui(self):
        p = self.parent
        p.configure(bg=BG)
        top = tk.Frame(p, bg=BG, pady=4)
        top.pack(fill="x", padx=8)
        tk.Label(top, text="Mai lezárt kereskedések  (MT5 szerver-idő)", bg=BG,
                 fg=FG_WHITE, font=self._header).pack(side="left")
        self._lbl_total = tk.Label(top, text="Összes P&L: —", bg=BG, fg=FG_WHITE,
                                   font=self._header)
        self._lbl_total.pack(side="right", padx=8)

        self._lbl_breakdown = tk.Label(p, text="", bg=BG, fg=FG_GRAY, font=self._small,
                                       anchor="w", justify="left")
        self._lbl_breakdown.pack(fill="x", padx=10, pady=(0, 4))

        hdr = tk.Frame(p, bg=BG_HEADER)
        hdr.pack(fill="x", padx=2)
        for key, label, w, anchor in CLOSED_COLUMNS:
            tk.Label(hdr, text=label, width=w, anchor=anchor, bg=BG_HEADER,
                     fg=FG_BLUE, font=self._header, padx=4, pady=3).pack(side="left")
        tk.Frame(p, bg=FG_GRAY_DIM, height=1).pack(fill="x", padx=2)

        holder = tk.Frame(p, bg=BG)
        holder.pack(fill="both", expand=True, padx=2)
        canvas = tk.Canvas(holder, bg=BG, highlightthickness=0)
        vsb = tk.Scrollbar(holder, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        self._rows_frame = tk.Frame(canvas, bg=BG)
        canvas.create_window((0, 0), window=self._rows_frame, anchor="nw")
        self._rows_frame.bind(
            "<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

    def refresh(self):
        # Napváltás (a provider csak a MAI trade-eket adja, a sorok viszont csak
        # bővültek) → új napon nulláznunk kell, különben két nap adata keveredne.
        today = datetime.now().date()
        if self._day is not None and today != self._day:
            for w in self._rows_frame.winfo_children():
                w.destroy()
            self._rows.clear()
        self._day = today

        closed = self._closed_provider() or []
        for c in closed:
            pid = c["position"]
            if pid not in self._rows:
                self._rows[pid] = self._make_row(c)   # nincs törlés — a lista csak bővül

        total   = sum(c["pnl"] for c in closed)
        wins    = sum(1 for c in closed if c["pnl"] > 0)
        losses  = sum(1 for c in closed if c["pnl"] < 0)
        r_vals  = [_r_multiple(c) for c in closed]
        total_r = sum(r for r in r_vals if r is not None)
        r_txt   = f"   |   {total_r:+.2f}R" if any(r is not None for r in r_vals) else ""
        self._lbl_total.config(
            text=f"Összes P&L: {total:+.2f}${r_txt}   |   {len(closed)} trade   |   {wins}W / {losses}L",
            fg=FG_GREEN if total >= 0 else FG_RED)

        by_strat: dict = {}
        for c in closed:
            nm = self._strategy_provider(c.get("magic")) if self._strategy_provider else "—"
            a = by_strat.setdefault(nm, [0.0, 0])
            a[0] += c["pnl"]
            a[1] += 1
        if by_strat:
            parts = [f"{s}: {v[0]:+.2f}$ ({v[1]})" for s, v in sorted(by_strat.items())]
            self._lbl_breakdown.config(text="   |   ".join(parts), fg=FG_GRAY)
        else:
            self._lbl_breakdown.config(text="Ma még nincs lezárt kereskedés.", fg=FG_GRAY)

    def _make_row(self, c):
        digits = self._digits_provider(c["symbol"])
        strat  = self._strategy_provider(c.get("magic")) if self._strategy_provider else "—"
        t      = c["type"]
        pnl    = c["pnl"]
        r      = _r_multiple(c)
        tstr   = datetime.fromtimestamp(c["close_time"], tz=timezone.utc).strftime("%H:%M")
        vals = {
            "symbol":   (c["symbol"],                       FG_WHITE),
            "strategy": (strat,                             FG_GRAY),
            "type":     (t,                 FG_GREEN if t == "BUY" else FG_RED),
            "volume":   (f'{c["volume"]:.2f}',              FG_WHITE),
            "open":     (_fmt_price(c["price_open"], digits),  FG_GRAY),
            "close":    (_fmt_price(c["price_close"], digits), FG_WHITE),
            "time":     (tstr,                              FG_GRAY),
            "pnl":      (f"{pnl:+.2f}$",     FG_GREEN if pnl >= 0 else FG_RED),
            "r":        (f"{r:+.2f}R" if r is not None else "—",
                         FG_GRAY if r is None else (FG_GREEN if r >= 0 else FG_RED)),
        }
        row = tk.Frame(self._rows_frame, bg=BG_ROW_EVEN)
        for key, label, w, anchor in CLOSED_COLUMNS:
            txt, fg = vals[key]
            tk.Label(row, text=txt, width=w, anchor=anchor, bg=BG_ROW_EVEN, fg=fg,
                     font=self._mono, padx=4, pady=2).pack(side="left")
        row.pack(fill="x", padx=2)
        return row


# ---------------------------------------------------------------------------
# Fő Dashboard ablak
# ---------------------------------------------------------------------------

class DashboardWindow:
    def __init__(self, cfg: dict, dashboard_ref: dict,
                 instrument_state: dict, optimizer_status: dict,
                 on_play_pair, on_stop_pair, strategy=None,
                 on_slots_change=None, auto_resume_opt=False):
        self.cfg              = cfg
        self._auto_resume_opt = auto_resume_opt
        self.dashboard_ref    = dashboard_ref
        self.instrument_state = instrument_state
        self.optimizer_status = optimizer_status
        self._on_play         = on_play_pair
        self._on_stop         = on_stop_pair
        self._on_slots_change = on_slots_change
        self.strategy         = strategy or get_strategy(cfg)
        # Több-stratégia: oszlop MINDEN ELÉRHETŐ stratégiához (fejléc = neve). Az
        # elérhetők a config `available_strategies` whitelistje (alap = az összes
        # regisztrált) — így egy kikapcsolt stratégia nem kap oszlopot.
        from strategy import available_strategy_names, get_strategy_by_name
        self._all_strategies  = [get_strategy_by_name(n)
                                 for n in available_strategy_names(cfg)]
        self._columns         = build_columns(self._all_strategies)
        # Stratégia-hatókörű params-tárolás: aktív stratégia + egyszeri migráció.
        from core.params_store import set_active_strategy, migrate_flat_layout
        set_active_strategy(self.strategy.name)
        migrate_flat_layout(self.strategy.name)

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
        self.root.title(f"{APP_NAME} v{APP_VERSION} — Live Dashboard")
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
        tk.Label(top_bar, text=APP_NAME,
                 bg=BG_HEADER, fg=FG_BLUE, font=title_font).pack(side="left", padx=(10, 3))
        # Verzió — jól látható helyen, a név mellett (build-azonosításhoz)
        tk.Label(top_bar, text=f"v{APP_VERSION}",
                 bg=BG_HEADER, fg=FG_CYAN, font=info_font).pack(side="left", padx=(0, 10))
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
        self.lbl_slots.pack(side="left", padx=(10, 2))
        # Max slotszám állítása a felületről (csökkenteni csak a foglaltakig lehet)
        tk.Button(info_bar, text="▼", font=small_font, width=2,
                  bg=BG_INACTIVE, fg=FG_WHITE, relief="flat", cursor="hand2",
                  command=lambda: self._change_slots(-1)).pack(side="left", padx=1)
        tk.Button(info_bar, text="▲", font=small_font, width=2,
                  bg=BG_INACTIVE, fg=FG_WHITE, relief="flat", cursor="hand2",
                  command=lambda: self._change_slots(+1)).pack(side="left", padx=(1, 10))
        self.lbl_limit   = tk.Label(info_bar, text="Napi limit: OK",
                                    bg=BG_HEADER, fg=FG_GREEN, font=info_font)
        self.lbl_limit.pack(side="left", padx=(10, 2))
        # Napi limit állítása a felületről (mint a slotoké): ▼/▲ 10$-os lépésben,
        # a config.json trading.daily_loss_limit_usd kulcsába perzisztálva. A live
        # motor UGYANEZT a cfg-dictet olvassa → azonnal él.
        tk.Button(info_bar, text="▼", font=small_font, width=2,
                  bg=BG_INACTIVE, fg=FG_WHITE, relief="flat", cursor="hand2",
                  command=lambda: self._change_daily_limit(-10)).pack(side="left", padx=1)
        tk.Button(info_bar, text="▲", font=small_font, width=2,
                  bg=BG_INACTIVE, fg=FG_WHITE, relief="flat", cursor="hand2",
                  command=lambda: self._change_daily_limit(+10)).pack(side="left", padx=(1, 10))

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
            on_panic=self._pos_panic, on_close_all=self._pos_close_all,
            on_name_click=self._show_instrument_params,
            on_trail_dist=self._pos_trail_dist,
            trail_default_provider=self._trail_default,
            point_provider=lambda sym: getattr(self.dashboard_ref.get(sym), "point", None),
            strategy_provider=self._strategy_by_magic,
            on_build=self._pos_build,
            on_build_mode=self._pos_build_mode)

        closed_frame = tk.Frame(self._notebook, bg=BG)
        self._notebook.add(closed_frame, text="  Lezárt (ma)  ")
        self._closed_tab = ClosedTab(
            closed_frame, mono_font, small_font, header_font,
            closed_provider=lambda: getattr(self, "_mt5_cache", {}).get("closed_today", []),
            strategy_provider=self._strategy_by_magic,
            digits_provider=lambda sym: getattr(self.dashboard_ref.get(sym), "digits", 5))

        bt_frame = tk.Frame(self._notebook, bg=BG_BT)
        self._notebook.add(bt_frame, text="  Portfólió Backtest  ")
        self._bt_tab = PortfolioBacktestTab(bt_frame, cfg, mono_font, small_font, header_font)

        self._balance    = 0.0
        self._free_slots = cfg["trading"]["max_open_slots"]
        self._max_slots  = cfg["trading"]["max_open_slots"]
        # A nyitott pozíciók BONTÁSA a slot-címkéhez: összes darab vs. ebből
        # ténylegesen slotot foglaló (nem kockázatmentes). Enélkül a „8 nyitott
        # pozíció 4 slot mellett" jogos gyanút kelt — pedig szabályos, ha a
        # többi már BE-re húzott. Lásd `_render_slots_label`.
        self._open_total    = 0
        self._open_occupied = 0

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
            ("Kockázatcsökk. (kattints):", FG_GRAY),
            ("R Risky", FG_ORANGE), ("F Felező", FG_CYAN), ("P Pajzs", FG_GREEN),
            ("Fi Fibo", FG_YELLOW), ("H Harmados", FG_PURPLE),
            ("PF Pajzs↔Fibo", FG_TEAL),
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
        # A fejléc a canvason KÍVÜL van, így alapból a scrollbar FÖLÖTT is
        # végigérne → jobb oldalt a scrollbar tényleges szélességével behúzzuk,
        # hogy az (expandáló) Opt státusz fejléc pontosan a sorok széléig érjen.
        vsb.bind("<Configure>",
                 lambda e: self._header_row.frame.pack_configure(padx=(2, 2 + e.width)))

        self._table_frame = tk.Frame(canvas, bg=BG)   # ide kerülnek a sorok
        _win = canvas.create_window((0, 0), window=self._table_frame, anchor="nw")
        self._table_frame.bind(
            "<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        # A canvas-ba ágyazott frame NEM veszi fel magától a canvas szélességét
        # (a create_window a kért méretet használja) → átméretezéskor ráhúzzuk,
        # így a sorok fill="x"-e tényleg az ablak széléig ér (az Opt státusz
        # expand-ja a maradék szélességet kapja). Kis ablaknál a természetes
        # (kért) szélesség marad, hogy a cellák ne nyomódjanak össze.
        canvas.bind(
            "<Configure>",
            lambda e: canvas.itemconfigure(
                _win, width=max(e.width, self._table_frame.winfo_reqwidth())))

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
                on_name_click=self._show_instrument_settings,
                mono_font=mono_font, small_font=small_font,
                on_status_click=self._show_opt_log, on_viz=self._handle_viz,
                on_marker_click=self._show_strategy_params,
                on_opt_menu=self._handle_opt_menu, on_trades=self._handle_trades,
                on_tfalign=self._show_tfalign_settings)
            self._bind_ctrl_width_sync(self.rows[symbol])

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

    def _bind_ctrl_width_sync(self, row):
        """A sor gombsor-keretének TÉNYLEGES pixel-szélességét a fejléc Vezérlés
        cellájára tükrözi (fix karakter-szélesség helyett pontos igazítás).
        Minden sor ugyanakkora gombsort kap, így bármelyik sor jó forrás."""
        row.ctrl_frame.bind(
            "<Configure>",
            lambda e: self._header_row.sync_ctrl_width(e.width))

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
        available: list = []      # háttérszálból töltődik: [(name, description), ...]
        shown_names: list = []    # a listbox aktuális soraival igazított névlista

        frame_lb = tk.Frame(popup, bg=BG)
        frame_lb.pack(padx=12, fill="both", expand=True)
        scrollbar = tk.Scrollbar(frame_lb)
        scrollbar.pack(side="right", fill="y")
        listbox = tk.Listbox(frame_lb, width=46, height=18, bg=BG_HEADER, fg=FG_WHITE,
                             selectbackground=BTN_OPT_BG, font=self._small_font,
                             relief="flat", yscrollcommand=scrollbar.set)
        listbox.pack(side="left", fill="both", expand=True)
        scrollbar.config(command=listbox.yview)

        def refresh_list(*_):
            q = search_var.get().upper()
            listbox.delete(0, "end")
            shown_names.clear()
            for name, desc in available:
                # Keresés névre ÉS leírásra is
                if q and q not in name.upper() and q not in (desc or "").upper():
                    continue
                label = f"{name:<12} {desc}" if desc else name
                listbox.insert("end", label)
                shown_names.append(name)
        search_var.trace_add("write", refresh_list)

        lbl_info = tk.Label(popup, text="Szimbólumok betöltése...", bg=BG,
                            fg=FG_GRAY, font=self._small_font)
        lbl_info.pack(pady=(4, 0))

        # MT5 szimbólum-lekérés HÁTTÉRSZÁLON; a UI-t after(0)-val frissítjük.
        def _load_syms():
            try:
                import MetaTrader5 as mt5
                syms = mt5.symbols_get()
                pairs = sorted(((s.name, getattr(s, "description", "")) for s in syms),
                               key=lambda x: x[0]) if syms else []
            except Exception:
                pairs = []
            result = [(n, d) for n, d in pairs if n not in in_config]

            def _apply():
                if not popup.winfo_exists():
                    return
                available[:] = result
                refresh_list()
                if not result:
                    lbl_info.config(
                        text="Minden MT5 szimbólum már szerepel a listában.", fg=FG_YELLOW)
                else:
                    lbl_info.config(text=f"{len(result)} elérhető szimbólum "
                                         f"(név + leírás).", fg=FG_GRAY)
            try:
                self.root.after(0, _apply)
            except Exception:
                pass
        threading.Thread(target=_load_syms, daemon=True, name="MT5Symbols").start()

        def add_selected():
            sel = listbox.curselection()
            if not sel:
                return
            self._add_instrument(shown_names[sel[0]])
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
            min_lot, lot_step = 0.01, 0.01
            description = ""
            try:
                import MetaTrader5 as _mt5
                from core.mt5_connector import MT5_LOCK
                with MT5_LOCK:
                    info = _mt5.symbol_info(symbol)
                if info:
                    description = getattr(info, "description", "") or ""
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
                    # Lot-korlátok a brókertől — enélkül az optimalizálás/backteszt elszáll
                    min_lot  = getattr(info, "volume_min", 0.01) or 0.01
                    lot_step = getattr(info, "volume_step", 0.01) or 0.01
            except Exception:
                pass
            try:
                self.root.after(
                    0, lambda: self._finalize_add_instrument(
                        symbol, pip_size, pv1_usd, spread_pips, description,
                        min_lot, lot_step))
            except Exception:
                pass
        threading.Thread(target=_work, daemon=True, name="MT5AddInstr").start()

    def _finalize_add_instrument(self, symbol, pip_size, pv1_usd, spread_pips,
                                 description="", min_lot=0.01, lot_step=0.01):
        """A fő szálon fut: config-írás + dashboard state + új tábla-sor."""
        if symbol in self.rows:
            return
        self.cfg["pairs"][symbol] = {
            "enabled": False, "pip_size": pip_size, "pv1_usd": pv1_usd,
            "min_lot": min_lot, "lot_step": lot_step,
            "backtest_spread_pips": spread_pips, "sess_start": 0, "sess_end": 24,
            "description": description,
        }
        self._save_main_config()

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
            on_name_click=self._show_instrument_settings,
            mono_font=self._mono_font, small_font=self._small_font,
            on_status_click=self._show_opt_log, on_viz=self._handle_viz,
            on_marker_click=self._show_strategy_params,
            on_opt_menu=self._handle_opt_menu, on_trades=self._handle_trades,
            on_tfalign=self._show_tfalign_settings)
        self._bind_ctrl_width_sync(self.rows[symbol])
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

    # ── config.json perzisztálás (CSAK a váz-szekciók) ───────────────────
    def _save_main_config(self):
        """A config.json-ba csak a VÁZ-szekciókat írjuk (a stratégia-config a
        saját fájljában él) — így a merge-elt futásidejű cfg nem szennyezi vissza."""
        try:
            with open(ROOT / "config.json", "w", encoding="utf-8") as f:
                json.dump(main_config_view(self.cfg), f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    # ── Beállítás-szerkesztő (config.json) ───────────────────────────────
    def _show_settings(self):
        popup = tk.Toplevel(self.root)
        popup.title("Beállítások — config.json")
        popup.configure(bg=BG)
        popup.geometry("720x640")
        popup.grab_set()
        tk.Label(popup, text="config.json szerkesztése (mentéskor JSON-validálás):",
                 bg=BG, fg=FG_BLUE, font=self._header_font).pack(anchor="w", padx=10, pady=(10, 2))
        tk.Label(popup, text="Megjegyzés: itt csak a VÁZ-config szerkeszthető. A stratégia "
                 "beállításai (indicators, sltp, position_mgmt, quality, optimizer-tér) a "
                 "stratégia saját fájljában élnek: strategy/config/<name>.json.",
                 bg=BG, fg=FG_GRAY, font=self._small_font, justify="left",
                 wraplength=680).pack(anchor="w", padx=10)

        # ── Elérhető stratégiák (config: available_strategies) ────────────────
        # A program ezeket kínálja fel (per-pár választó) és ezekből képez oszlopot.
        # A jelölőnégyzetek AZ IRÁNYADÓK erre a kulcsra (a lenti JSON-t felülírják).
        from strategy import registered_strategy_names, available_strategy_names
        tk.Label(popup, text="Elérhető stratégiák (a program ezeket kínálja és ezekből "
                 "képez oszlopot — az oszlop-változás újraindítás után látszik):",
                 bg=BG, fg=FG_BLUE, font=self._small_font, justify="left",
                 wraplength=680).pack(anchor="w", padx=10, pady=(8, 0))
        _avail_now  = set(available_strategy_names(self.cfg))
        _avail_vars = {}
        _av_row = tk.Frame(popup, bg=BG)
        _av_row.pack(anchor="w", padx=20, pady=(2, 0))
        for _sn in registered_strategy_names():
            _v = tk.BooleanVar(value=(_sn in _avail_now))
            _avail_vars[_sn] = _v
            tk.Checkbutton(_av_row, text=_sn, variable=_v, bg=BG, fg=FG_WHITE,
                           selectcolor=BG_HEADER, font=self._small_font,
                           activebackground=BG, activeforeground=FG_WHITE).pack(
                           side="left", padx=(0, 12))

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
        # Csak a VÁZ-config látszik/szerkeszthető; a stratégia beállításai a
        # stratégia saját fájljában élnek (strategy/config/<name>.json).
        text.insert("1.0", json.dumps(main_config_view(self.cfg), indent=2, ensure_ascii=False))
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
            # Az 'Elérhető stratégiák' jelölőnégyzetek az irányadók az
            # available_strategies kulcsra (a szerkesztő JSON-ját felülírják).
            chosen_av = [n for n in registered_strategy_names() if _avail_vars[n].get()]
            if not chosen_av:
                lbl_err.config(text="Legalább egy stratégia legyen elérhető.")
                return
            if set(chosen_av) == set(registered_strategy_names()):
                new.pop("available_strategies", None)   # mind → default, ne szennyezze
            else:
                new["available_strategies"] = chosen_av
            try:
                with open(ROOT / "config.json", "w", encoding="utf-8") as f:
                    json.dump(new, f, indent=2, ensure_ascii=False)
            except Exception as e:
                lbl_err.config(text=f"Mentési hiba: {e}")
                return
            # In-place frissítés → a live_trader ugyanazt a dict-et látja.
            # A `new` a VÁZ-config; a stratégia beállításait újra beolvasztjuk,
            # hogy a merge-elt futásidejű cfg (indicators/quality/…) megmaradjon.
            self.cfg.clear()
            self.cfg.update(new)
            apply_strategy_config(self.cfg)
            popup.destroy()

        btns = tk.Frame(popup, bg=BG)
        btns.pack(pady=10)
        tk.Button(btns, text="Mentés", bg=BTN_PLAY_BG, fg=BTN_PLAY_FG, relief="flat",
                  font=self._small_font, command=save).pack(side="left", padx=6)
        tk.Button(btns, text="Mégse", bg=BTN_DIS_BG, fg=BTN_DIS_FG, relief="flat",
                  font=self._small_font, command=popup.destroy).pack(side="left", padx=6)

    # ── Stratégia Paraméterek (a KÖRRE kattintva — az adott stratégiáé) ──
    def _show_strategy_params(self, symbol: str, strategy_name: str = ""):
        """A jelölő-körre kattintva az ADOTT stratégia paraméter-ablaka nyílik.
        Optimalizálatlan párnál is nyílik (alap-paraméterek); a Mentés létrehozza
        a data/optimized_params/<strategy>/<symbol>.json-t."""
        from dashboard.instrument_dialog import InstrumentParamsDialog
        from strategy import get_strategy_by_name
        strat = get_strategy_by_name(strategy_name) if strategy_name else self.strategy
        InstrumentParamsDialog(
            self.root, symbol, self.cfg, strat,
            self._header_font, self._small_font, self._save_main_config)

    def _show_instrument_params(self, symbol: str):
        """Visszafelé komp.: az elsődleges stratégia paraméterei (a Pozíciók fül
        a Symbol-névre kattintva ezt hívja)."""
        self._show_strategy_params(symbol, self.strategy.name)

    # ── Instrumentum beállítások (az instrumentum NEVÉRE kattintva) ──────
    def _show_instrument_settings(self, symbol: str):
        """Mely stratégiák aktívak ezen az instrumentumon (több is választható →
        pairs.<sym>.strategies). A per-stratégia kockázatcsökkentés és a V-gomb
        stratégiája később kerül ide (a jegyzet szerint tisztázandó)."""
        from strategy import (available_strategy_names, enabled_strategy_names,
                              default_strategy_name)
        popup = tk.Toplevel(self.root)
        popup.title(f"{symbol} — instrumentum beállítások")
        popup.configure(bg=BG)
        popup.grab_set()
        tk.Label(popup, text=symbol, bg=BG, fg=FG_WHITE,
                 font=self._header_font).pack(anchor="w", padx=12, pady=(12, 2))
        tk.Label(popup, text="Aktív stratégiák ezen az instrumentumon (több is "
                             "választható):", bg=BG, fg=FG_GRAY,
                 font=self._small_font).pack(anchor="w", padx=12, pady=(4, 2))
        cur = set(enabled_strategy_names(self.cfg, symbol))
        _vars = {}
        for name in available_strategy_names(self.cfg):
            v = tk.BooleanVar(value=(name in cur))
            _vars[name] = v
            tk.Checkbutton(popup, text=name, variable=v, bg=BG, fg=FG_WHITE,
                           selectcolor=BG_HEADER, font=self._small_font,
                           activebackground=BG, activeforeground=FG_WHITE).pack(
                           anchor="w", padx=20)

        # ── Piac-előszűrő (piac-állapot osztályozó) — instrumentumonként EGY ──
        from core import market_strategy as _ms
        _pc0 = (self.cfg.get("pairs", {}).get(symbol, {}) or {})
        tk.Label(popup, text="Piac-előszűrő (piac-állapot osztályozó — 1/instrumentum):",
                 bg=BG, fg=FG_GRAY, font=self._small_font).pack(anchor="w", padx=12, pady=(8, 2))
        ms_var = tk.StringVar(value=(_ms.market_name_of(_pc0) or "Nincs"))
        _om = tk.OptionMenu(popup, ms_var, *(["Nincs"] + _ms.registered_market_names()))
        _om.config(bg=BG_HEADER, fg=FG_WHITE, font=self._small_font, relief="flat",
                   highlightthickness=0, activebackground=BG_HEADER)
        _om["menu"].config(bg=BG_HEADER, fg=FG_WHITE)
        _om.pack(anchor="w", padx=20)
        viz_var = tk.BooleanVar(value=bool(_pc0.get("market_viz", True)))
        tk.Checkbutton(popup, text="Piac-állapot sáv a charton (Viz)", variable=viz_var,
                       bg=BG, fg=FG_WHITE, selectcolor=BG_HEADER, font=self._small_font,
                       activebackground=BG, activeforeground=FG_WHITE).pack(anchor="w", padx=20)

        lbl = tk.Label(popup, text="", bg=BG, fg=FG_GRAY, font=self._small_font,
                       wraplength=360, justify="left")
        lbl.pack(anchor="w", padx=12, pady=(8, 0))

        def _save():
            chosen = [n for n in available_strategy_names(self.cfg) if _vars[n].get()]
            if not chosen:
                lbl.config(text="Legalább egy stratégia legyen aktív.", fg=FG_RED)
                return
            pc = self.cfg.setdefault("pairs", {}).setdefault(symbol, {})
            # Ha csak az elsődleges → ne szennyezzük a configot (default viselkedés).
            if chosen == [default_strategy_name(self.cfg)]:
                pc.pop("strategies", None)
            else:
                pc["strategies"] = chosen
            # Piac-előszűrő + chart-sáv kapcsoló
            msname = ms_var.get()
            if msname in ("Nincs", "", "none"):
                pc.pop("market_strategy", None)
            else:
                pc["market_strategy"] = msname
            if viz_var.get():
                pc.pop("market_viz", None)     # True az alap → ne szennyezze
            else:
                pc["market_viz"] = False
            # A tábla szürkítése/piac-cellája azonnal követi (a következő frissítéskor);
            # a KERESKEDÉS a következő botindításkor veszi át.
            ds = self.dashboard_ref.get(symbol)
            if ds is not None:
                ds.enabled_strategies = chosen
                ds.market_strategy = pc.get("market_strategy")
            try:
                self._save_main_config()
                _mstxt = msname if msname != "Nincs" else "nincs piac-előszűrő"
                lbl.config(text=f"Mentve: {', '.join(chosen)} | piac: {_mstxt}. A tábla "
                                f"azonnal követi; a kereskedés a következő botindításkor.",
                           fg=FG_GREEN)
            except Exception as ex:
                lbl.config(text=f"Mentési hiba: {ex}", fg=FG_RED)

        btns = tk.Frame(popup, bg=BG)
        btns.pack(pady=10)
        tk.Button(btns, text="Mentés", bg=BTN_PLAY_BG, fg=BTN_PLAY_FG, relief="flat",
                  font=self._small_font, command=_save).pack(side="left", padx=6)
        tk.Button(btns, text="Bezárás", bg=BTN_DIS_BG, fg=BTN_DIS_FG, relief="flat",
                  font=self._small_font, command=popup.destroy).pack(side="left", padx=6)

    # ── Opt státusz részletek (a státusz-cellára kattintva) ──────────────
    @staticmethod
    def _read_opt_log_for(symbol: str, max_blocks: int = 6) -> str:
        """Az adott instrumentumhoz tartozó legutóbbi hiba-blokkok az opt_error.log-ból."""
        log_file = ROOT / "data" / "opt_error.log"
        if not log_file.exists():
            return ""
        try:
            with open(log_file, encoding="utf-8") as f:
                raw = f.read()
        except Exception:
            return ""
        sep = "=" * 60
        blocks = [b for b in raw.split(sep) if f"[{symbol}]" in b]
        return sep.join(blocks[-max_blocks:]).strip() if blocks else ""

    def _show_opt_log(self, symbol: str):
        """Részletes optimalizálási állapot: státusz + trials CSV + hibalog."""
        popup = tk.Toplevel(self.root)
        popup.title(f"{symbol} — Optimalizálás állapota")
        popup.configure(bg=BG)
        popup.geometry("780x540")
        popup.grab_set()

        state  = self.instrument_state.get(symbol, "—")
        status = self.optimizer_status.get(symbol, "—")
        tk.Label(popup, text=f"{symbol}   —   állapot: {state}", bg=BG, fg=FG_BLUE,
                 font=self._header_font).pack(anchor="w", padx=10, pady=(10, 2))
        tk.Label(popup, text=f"Státusz (teljes): {status}", bg=BG, fg=FG_WHITE,
                 font=self._small_font, anchor="w", justify="left",
                 wraplength=740).pack(anchor="w", padx=10)

        # Trials CSV állapota + megnyitás
        from core.params_store import trials_file
        csv = trials_file(symbol)
        row = tk.Frame(popup, bg=BG)
        row.pack(anchor="w", padx=10, pady=(6, 0))
        if csv.exists():
            try:
                with open(csv, encoding="utf-8-sig") as f:
                    n = max(0, sum(1 for _ in f) - 1)
            except Exception:
                n = "?"
            tk.Label(row, text=f"Trials CSV: {n} sor  —  {csv.name}", bg=BG,
                     fg=FG_GREEN, font=self._small_font).pack(side="left")
            tk.Button(row, text="Megnyitás", bg=BTN_BT_BG, fg=BTN_BT_FG, relief="flat",
                      font=self._small_font,
                      command=lambda: self._open_file(csv)).pack(side="left", padx=8)
        else:
            tk.Label(row, text="Trials CSV: nincs (még nem futott le, vagy egy trial "
                               "sem készült el).", bg=BG, fg=FG_YELLOW,
                     font=self._small_font).pack(side="left")

        tk.Label(popup, text="Legutóbbi hibák/események (data/opt_error.log):",
                 bg=BG, fg=FG_GRAY, font=self._small_font).pack(anchor="w", padx=10, pady=(8, 0))

        txt_frame = tk.Frame(popup, bg=BG)
        txt_frame.pack(fill="both", expand=True, padx=10, pady=4)
        sb = tk.Scrollbar(txt_frame)
        sb.pack(side="right", fill="y")
        text = tk.Text(txt_frame, bg=BG_HEADER, fg=FG_WHITE, insertbackground=FG_WHITE,
                       font=self._mono_font, wrap="word", yscrollcommand=sb.set)
        text.pack(side="left", fill="both", expand=True)
        sb.config(command=text.yview)
        content = self._read_opt_log_for(symbol)
        text.insert("1.0", content or "(Nincs naplózott hiba ehhez az instrumentumhoz. "
                                      "Ha a Trials CSV létezik, az optimalizálás lefutott — "
                                      "nyisd meg és nézd meg a score oszlopot.)")
        text.config(state="disabled")

        tk.Button(popup, text="Bezár", bg=BTN_DIS_BG, fg=BTN_DIS_FG, relief="flat",
                  font=self._small_font, command=popup.destroy).pack(pady=8)

    @staticmethod
    def _open_file(path):
        try:
            import os
            os.startfile(str(path))
        except Exception:
            pass

    # ── Gomb handlerek ────────────────────────────────────────────────────
    def _handle_run(self, symbol: str):
        """A futtató gomb (Play↔Stop morph) kezelője."""
        st = self.instrument_state.get(symbol)
        if st == "STOPPED":
            self._handle_play(symbol)
        elif st == "LIVE":
            self._handle_stop(symbol)

    def _persist_run_state(self, symbol: str, state: str):
        """A kereskedés-SZÁNDÉK perzisztálása a config.json-ba (restart-biztos):
        a szimbólum engedélyezett stratégiáira beállítja a `run_state`-et (+ az
        `enabled`-et szinkronban), majd ment. Így újraindításkor a `run()` a
        korábban futó párokat magától LIVE-ba teszi."""
        try:
            from core import run_state as _rs
            from strategy import enabled_strategy_names
            strat_names = enabled_strategy_names(self.cfg, symbol) or [self.strategy.name]
            for sn in strat_names:
                _rs.set_state(self.cfg, symbol, sn, state)
            self._save_main_config()
        except Exception:
            pass

    def _handle_play(self, symbol: str):
        ds = self.dashboard_ref.get(symbol)
        if ds is None or not ds.trained:
            return
        if self.instrument_state.get(symbol) != "STOPPED":
            return
        self.instrument_state[symbol] = "LIVE"
        self._persist_run_state(symbol, "live")      # restart után folytassa a kereskedést
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
        self._persist_run_state(symbol, "stopped")   # restart után NE induljon magától
        if self._on_stop:
            self._on_stop(symbol)

    def _opt_strategies_for(self, symbol: str) -> list:
        """Az instrumentumon OPTIMALIZÁLHATÓ stratégiák (az engedélyezettek; ha nincs
        explicit lista, az elsődleges/aktív)."""
        from strategy import enabled_strategy_names
        return enabled_strategy_names(self.cfg, symbol) or [self.strategy.name]

    def _handle_opt(self, symbol: str):
        """OPT↔STOP morph. STOPPED → EGY stratégiánál azonnal indít; TÖBB engedélyezett
        stratégiánál VÁLASZTÓ-MENÜ nyílik (melyiket — vagy mindet), hogy egyértelmű
        legyen, mi fog futni (az ml_ai-nál az Opt = tanítás!). QUEUED → a szimbólum
        sorban álló tételeinek törlése."""
        st = self.instrument_state.get(symbol)
        if st == "STOPPED":
            names = self._opt_strategies_for(symbol)
            if len(names) > 1:
                # A menü az OPT gomb alá nyílik (gomb-kattintásnál nincs event-koordináta)
                row = self.rows.get(symbol)
                btn = getattr(row, "btn_opt", None)
                x = btn.winfo_rootx() if btn else self.root.winfo_pointerx()
                y = (btn.winfo_rooty() + btn.winfo_height()) if btn \
                    else self.root.winfo_pointery()
                self._show_opt_menu(symbol, names, x, y)
                return
            for sn in names:
                self._opt_ctrl.request_optimize(symbol, sn)
        elif st == "QUEUED":
            self._opt_ctrl.cancel_queued(symbol)
        elif st == "OPTIMIZING":
            # FUTÓ optimalizálás/tanítás leállítása (stop-marker → trial-határon
            # áll le; az eredmény eldobva, a mentett paraméterek érintetlenek).
            self._opt_ctrl.request_stop(symbol)
        else:
            return
        self._refresh_row(symbol)

    def _show_opt_menu(self, symbol: str, names: list, x: int, y: int):
        """Stratégiaválasztó menü az optimalizáláshoz (bal-klikk több stratégiánál
        és jobb-klikk is ezt használja). Az ml_ai-féle tanítható stratégiát a
        felirat is jelzi (Opt = tanítás)."""
        from strategy import get_strategy_by_name
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label=f"— {symbol} optimalizálása —", state="disabled")
        for sn in names:
            trainable = callable(getattr(get_strategy_by_name(sn), "fit", None))
            label = f"▶ {sn} (tanítás)" if trainable else f"▶ {sn}"
            menu.add_command(
                label=label,
                command=lambda s=sn: (self._opt_ctrl.request_optimize(symbol, s),
                                      self._refresh_row(symbol)))
        if len(names) > 1:
            menu.add_separator()
            menu.add_command(
                label="▶ Mind",
                command=lambda: ([self._opt_ctrl.request_optimize(symbol, s)
                                  for s in names], self._refresh_row(symbol)))
        # Opt-állapot (study/trials/marker) törlése — per stratégia, majd 'Mind'.
        menu.add_separator()
        if len(names) == 1:
            menu.add_command(label="🗑 Opt-állapot törlése",
                             command=lambda: self._delete_opt_state(symbol, list(names)))
        else:
            for sn in names:
                menu.add_command(
                    label=f"🗑 {sn} opt-állapot törlése",
                    command=lambda s=sn: self._delete_opt_state(symbol, [s]))
            menu.add_command(label="🗑 Mind törlése",
                             command=lambda: self._delete_opt_state(symbol, list(names)))
        try:
            menu.tk_popup(int(x), int(y))
        finally:
            menu.grab_release()

    def _delete_opt_state(self, symbol: str, names: list):
        """Az optimalizálási ÁLLAPOT/LOG törlése a megadott stratégiákhoz: study DB
        (optuna SQLite, a folytatás forrása), trials CSV, done- és stop-marker. A
        mentett paraméterek (params) MEGMARADNAK — csak az előzmény és az 'Utolsó opt'
        dátum tűnik el, így a következő futás tiszta lappal (nem folytatás) indul.
        Futó/sorban álló optimalizálásnál tiltva."""
        from tkinter import messagebox
        if self.instrument_state.get(symbol) in ("OPTIMIZING", "QUEUED"):
            messagebox.showinfo(
                "Nem törölhető",
                "Előbb állítsd le a futó/sorban álló optimalizálást.")
            return
        who = ", ".join(names)
        if not messagebox.askyesno(
                "Opt-állapot törlése",
                f"Törlöd a(z) {symbol} optimalizálási állapotát ({who})?\n\n"
                "A mentett paraméterek megmaradnak, de a study/trials-előzmény és az "
                "'Utolsó opt' dátum törlődik — a következő futás nem folytatás lesz."):
            return
        from core.params_store import (trials_file, study_db, done_marker,
                                        stop_marker)
        for sn in names:
            for pth in (trials_file(symbol, sn), study_db(symbol, sn),
                        done_marker(symbol, sn), stop_marker(symbol, sn)):
                try:
                    if pth.exists():
                        pth.unlink()
                except Exception:
                    pass
        self.optimizer_status[symbol] = ""
        self._refresh_row(symbol)

    def _handle_opt_menu(self, symbol: str, event):
        """JOBB-klikk az OPT gombon: stratégiaválasztó menü (ugyanaz, mint a
        bal-klikk több stratégiánál). LIVE szimbólumnál nem teszünk semmit."""
        if self.instrument_state.get(symbol) == "LIVE":
            return
        self._show_opt_menu(symbol, self._opt_strategies_for(symbol),
                            event.x_root, event.y_root)

    def _refresh_row(self, symbol: str):
        row = self.rows.get(symbol)
        ds  = self.dashboard_ref.get(symbol)
        if row and ds:
            row.update(ds, self.instrument_state.get(symbol, "STOPPED"),
                       self.optimizer_status.get(symbol, ""),
                       connected=getattr(self, "_connected", True))

    def _handle_risky(self, symbol: str):
        """Az „R" gomb: a kockázatcsökkentő PRESET körbe-váltása
        (Ki → Risky → Felező → Pajzs → Fibo → Harmados), per-pár mentve
        (data/risk_mode.json).
        A régi risky_mode-ot szinkronban tartjuk (preset==risky), hogy az azt
        olvasó live/backtest változatlanul működjön."""
        from core import rr_state, risky_mode, risk_reduction as _rr
        preset = rr_state.cycle_preset(symbol)
        risky_mode.set_risky(symbol, preset == _rr.PRESET_RISKY)
        ds = self.dashboard_ref.get(symbol)
        if ds is not None:
            ds.rr_preset = preset
            ds.risky = (preset == _rr.PRESET_RISKY)
            row = self.rows.get(symbol)
            if row is not None:
                no_trade = (self.instrument_state.get(symbol) == "LIVE"
                            and self._is_no_trade_now(symbol))
                row.update(ds, self.instrument_state.get(symbol, "STOPPED"),
                           self.optimizer_status.get(symbol, ""),
                           connected=getattr(self, "_connected", False),
                           no_trade=no_trade)

    def _handle_viz(self, symbol: str):
        """Vizualizáció ki/be az adott instrumentumhoz. Bekapcsoláskor azonnali
        újrarajzolás (a throttle nullázásával); kikapcsoláskor a chart-objektumok
        törlése (mt5_visual.clear)."""
        ds = self.dashboard_ref.get(symbol)
        if ds is None:
            return
        ds.viz_enabled = not getattr(ds, "viz_enabled", True)
        try:
            from trading import live_trader as _lt
            from core import mt5_visual as _viz
            if ds.viz_enabled:
                _lt._viz_last_write.pop(symbol, None)   # következő ciklusban azonnal ír
            else:
                _viz.clear(symbol)                       # objektumok törlése a chartról
        except Exception:
            pass
        # Perzisztálás a config.json-ba (per-instrumentum V állapot)
        try:
            pc = self.cfg.get("pairs", {}).get(symbol)
            if isinstance(pc, dict):
                pc["viz_enabled"] = ds.viz_enabled
                self._save_main_config()
        except Exception:
            pass
        row = self.rows.get(symbol)
        if row is not None:
            row.update(ds, self.instrument_state.get(symbol, "STOPPED"),
                       self.optimizer_status.get(symbol, ""),
                       connected=getattr(self, "_connected", False))

    def _handle_trades(self, symbol: str):
        """A JEL-REPLAY réteg (a sűrű zöld/piros belépő-jelzés vonalak + Entry/TP/SL)
        ki/be az adott instrumentumhoz. A tényleges MT5-kötések ettől függetlenül
        látszanak. A no-delete viz-modell miatt a következő íráshoz egyszeri CLEAR-t
        kérünk, hogy a jel-objektumok tisztán eltűnjenek / újrarajzolódjanak."""
        ds = self.dashboard_ref.get(symbol)
        if ds is None:
            return
        ds.show_trades = not getattr(ds, "show_trades", True)
        try:
            from trading import live_trader as _lt
            _lt._viz_pending_clear[symbol] = True    # tiszta újrarajz (kötések be/ki)
            _lt._viz_last_write.pop(symbol, None)      # következő ciklusban azonnal ír
        except Exception:
            pass
        # Perzisztálás a config.json-ba (per-instrumentum K állapot)
        try:
            pc = self.cfg.get("pairs", {}).get(symbol)
            if isinstance(pc, dict):
                pc["show_trades"] = ds.show_trades
                self._save_main_config()
        except Exception:
            pass
        row = self.rows.get(symbol)
        if row is not None:
            row.update(ds, self.instrument_state.get(symbol, "STOPPED"),
                       self.optimizer_status.get(symbol, ""),
                       connected=getattr(self, "_connected", False))

    def _show_tfalign_settings(self, symbol: str):
        """A TF-együttállás beállításai az ADOTT instrumentumra (per-pár): mely
        idősíkokat figyelje (2–6) + SMA-periódus + be/ki, ÉS stratégiánként egy
        kapcsoló, hogy az együttállás AKADÁLYOZZA-e a belépőt (csak a trenddel
        egyező jel köthet). Mentés a `pairs.<sym>.tf_align`-ba. Az „Együtt" cellára
        kattintva nyílik."""
        from core import tf_align as _tfa
        from strategy import available_strategy_names
        _en, _tfs, _sma, _gate = _tfa.config_for(self.cfg, symbol)
        popup = tk.Toplevel(self.root)
        popup.title(f"{symbol} — TF-együttállás")
        popup.configure(bg=BG)
        popup.resizable(False, False)
        popup.grab_set()
        tk.Label(popup, text=f"{symbol} — TF-együttállás figyelő", bg=BG, fg=FG_WHITE,
                 font=self._header_font).pack(anchor="w", padx=12, pady=(12, 2))
        tk.Label(popup, text="Mely idősíkokat figyelje (válassz 2–6-ot):", bg=BG,
                 fg=FG_GRAY, font=self._small_font).pack(anchor="w", padx=12, pady=(4, 2))
        _AVAIL = [(1, "M1"), (5, "M5"), (15, "M15"), (30, "M30"), (60, "H1"), (240, "H4")]
        _vars = {}
        _row = tk.Frame(popup, bg=BG)
        _row.pack(anchor="w", padx=18)
        for mins, lbl in _AVAIL:
            v = tk.BooleanVar(value=(mins in _tfs))
            _vars[mins] = v
            tk.Checkbutton(_row, text=lbl, variable=v, bg=BG, fg=FG_WHITE,
                           selectcolor=BG_HEADER, font=self._small_font,
                           activebackground=BG, activeforeground=FG_WHITE).pack(
                           side="left", padx=(0, 10))

        _f2 = tk.Frame(popup, bg=BG)
        _f2.pack(anchor="w", padx=12, pady=(8, 2))
        tk.Label(_f2, text="SMA-periódus:", bg=BG, fg=FG_GRAY,
                 font=self._small_font).pack(side="left")
        sma_var = tk.StringVar(value=str(_sma))
        tk.Entry(_f2, textvariable=sma_var, width=6, bg=BG_HEADER, fg=FG_WHITE,
                 font=self._small_font, insertbackground=FG_WHITE).pack(side="left", padx=6)
        en_var = tk.BooleanVar(value=_en)
        tk.Checkbutton(popup, text="Bekapcsolva (az oszlop + viz-SMA látszik)", variable=en_var,
                       bg=BG, fg=FG_WHITE, selectcolor=BG_HEADER, font=self._small_font,
                       activebackground=BG, activeforeground=FG_WHITE).pack(
                       anchor="w", padx=12, pady=(4, 0))

        # ── Kapu: az együttállás akadályozza-e a belépőt (per stratégia) ──────
        tk.Label(popup, text="Az együttállás AKADÁLYOZZA a belépőt (csak a trenddel "
                 "egyező jel köthet) ezeknél a stratégiáknál:", bg=BG, fg=FG_GRAY,
                 font=self._small_font, justify="left", wraplength=340).pack(
                 anchor="w", padx=12, pady=(10, 2))
        _gate_vars = {}
        _grow = tk.Frame(popup, bg=BG)
        _grow.pack(anchor="w", padx=18)
        for sn in available_strategy_names(self.cfg):
            gv = tk.BooleanVar(value=(sn in _gate))
            _gate_vars[sn] = gv
            tk.Checkbutton(_grow, text=sn, variable=gv, bg=BG, fg=FG_WHITE,
                           selectcolor=BG_HEADER, font=self._small_font,
                           activebackground=BG, activeforeground=FG_WHITE).pack(
                           side="left", padx=(0, 12))

        lbl_err = tk.Label(popup, text="", bg=BG, fg=FG_RED, font=self._small_font,
                           wraplength=340, justify="left")
        lbl_err.pack(anchor="w", padx=12, pady=(6, 0))

        def _save():
            chosen = [m for m, _ in _AVAIL if _vars[m].get()]
            if not (2 <= len(chosen) <= 6):
                lbl_err.config(text="2–6 idősíkot válassz.")
                return
            try:
                sma = max(2, int(sma_var.get().strip()))
            except ValueError:
                lbl_err.config(text="Az SMA-periódus egész szám legyen.")
                return
            gate = [sn for sn, gv in _gate_vars.items() if gv.get()]
            pc = self.cfg.setdefault("pairs", {}).setdefault(symbol, {})
            pc["tf_align"] = {"enabled": bool(en_var.get()),
                              "timeframes": chosen, "sma_period": sma, "gate": gate}
            self._save_main_config()
            popup.destroy()

        btns = tk.Frame(popup, bg=BG)
        btns.pack(pady=12)
        tk.Button(btns, text="Mentés", bg=BTN_PLAY_BG, fg=BTN_PLAY_FG, relief="flat",
                  font=self._small_font, command=_save).pack(side="left", padx=6)
        tk.Button(btns, text="Mégse", bg=BTN_DIS_BG, fg=BTN_DIS_FG, relief="flat",
                  font=self._small_font, command=popup.destroy).pack(side="left", padx=6)

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
        self._save_main_config()
        row = self.rows.pop(symbol, None)
        if row is not None:
            row.frame.destroy()
        self.dashboard_ref.pop(symbol, None)
        self.instrument_state.pop(symbol, None)
        self.optimizer_status.pop(symbol, None)
        self._apply_filter_sort()

    def _strategy_by_magic(self, magic) -> str:
        """magic → stratégianév (Pozíciók / Lezárt fül). Jelenleg egy stratégia
        van; több stratégiánál itt bővül a leképezés (magic-onként egyedi)."""
        try:
            if magic is not None and int(magic) == self.strategy.magic(self.cfg):
                return self.strategy.name
        except Exception:
            pass
        return "—"

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
            import logging as _logging
            from core import mt5_connector
            from trading.live_trader import position_state
            _log = _logging.getLogger(__name__)
            # Költség-tudatos BE (spread + jutalék + swap fedezve). Ha az ár még
            # nincs elég messze a nettó ≥ 0-hoz, a hívás False-t ad → NEM BE-zünk.
            if mt5_connector.move_to_breakeven(ticket):
                st = position_state.setdefault(
                    ticket, {"original_sl": orig_sl, "trailing_enabled": True,
                             "be_done": False, "trail_points": None, "trail_moved": False})
                st["be_done"] = True
                _log.info("✦ #%d — kézi költség-tudatos breakeven beállítva", ticket)
            else:
                # A gomb rendes esetben tiltva van ilyenkor; ha mégis idejut (a
                # háttér-frissítés és a kattintás közti ár-mozgás miatt), csak logol.
                _log.info("#%d — BE még nem lehetséges (az ár nem fedezi a "
                          "spread+jutalék+swap költséget) → SL változatlan", ticket)
        threading.Thread(target=_w, daemon=True, name="ManualBE").start()

    def _pos_build(self, ticket: int):
        """A „＋" gomb: kézi ráépítés a ticket SZIMBÓLUMÁRA (a motor manual_build-jét
        hívja háttérszálon — az nyit egy piramidális adalékot + átlagár-stopokat)."""
        pos = next((p for p in getattr(self, "_mt5_cache", {}).get("positions_detail", [])
                    if p["ticket"] == ticket), None)
        if not pos:
            return
        symbol = pos["symbol"]
        def _w():
            import logging as _logging
            from trading.live_trader import manual_build
            if not manual_build(symbol):
                _logging.getLogger(__name__).info(
                    "%s — ráépítés kihagyva (nincs érvényes építés-jel).", symbol)
        threading.Thread(target=_w, daemon=True, name="ManualBuild").start()

    def _pos_build_mode(self, symbol: str):
        """Az „Ép:" gomb: a SZIMBÓLUM építés-módját körbe-váltja (Ki → Kézi → Auto),
        mint az instrumentum-ablak Építés-választója. A motor a következő ciklusban a
        build_runtime-ot ehhez igazítja (a „＋" akkortól él Kézinél)."""
        try:
            from core import build_state as _bst
            _bst.cycle_mode(symbol)
        except Exception:
            pass

    _DEFAULT_PSTATE = {"original_sl": 0.0, "trailing_enabled": True,
                       "be_done": False, "trail_points": None, "trail_moved": False}

    def _pos_trail(self, ticket: int):
        from trading.live_trader import position_state
        st = position_state.setdefault(ticket, dict(self._DEFAULT_PSTATE))
        st["trailing_enabled"] = not st.get("trailing_enabled", True)

    def _pos_trail_dist(self, ticket: int, points: int):
        """Kézi trail-távolság beállítása egy ticketre PONTBAN (Pozíciók fül)."""
        from trading.live_trader import position_state
        st = position_state.setdefault(ticket, dict(self._DEFAULT_PSTATE))
        st["trail_points"] = points

    def _trail_default(self, symbol: str) -> Optional[int]:
        """Egy szimbólum optimalizált trail-távolsága PONTBAN (a Pozíciók fül
        mezőjéhez). A paraméter pipben van tárolva → pont = pip × pip_size / point.
        Ha a 'point' még nem ismert (MT5 adat nélkül), None-t ad."""
        from core.params_store import params_file
        pips = None
        pf = params_file(symbol)
        if pf.exists():
            try:
                with open(pf, encoding="utf-8") as f:
                    params = json.load(f).get("params", {})
                pips = params.get("trail_distance_pips")
            except Exception:
                pass
        if pips is None:
            pips = self.cfg.get("position_mgmt", {}).get("trail_distance_pips")
        if pips is None:
            return None
        pair_cfg = self.cfg["pairs"].get(symbol, {})
        pip_size = pair_cfg.get("pip_size")
        ds = self.dashboard_ref.get(symbol)
        point = getattr(ds, "point", None) if ds else None
        # On-demand `point`: ha a szimbólum még nem streamelt point-ot (pl. nem aktívan
        # pollozott pár, de VAN rajta nyitott pozíció — pl. SP500), közvetlenül lekérjük
        # az MT5-ből és cache-eljük a ds-be. Enélkül a trail-távolság mező üres marad.
        if not point:
            try:
                import MetaTrader5 as _mt5
                from core.mt5_connector import MT5_LOCK
                with MT5_LOCK:
                    _mt5.symbol_select(symbol, True)
                    _info = _mt5.symbol_info(symbol)
                if _info and _info.point > 0:
                    point = _info.point
                    if ds is not None:
                        ds.point = point
                    # pip_size hiánynál (nem konfigurált pár) heurisztikus tartalék a
                    # digits alapján (mint a light-poll), hogy a mező akkor is kiírjon.
                    if not pip_size and _info:
                        d = getattr(_info, "digits", 5)
                        pip_size = point * (10 if d in (3, 5) else 100 if d == 6 else 1)
            except Exception:
                pass
        if not pip_size or not point:
            return None
        return int(round(float(pips) * pip_size / point))

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

    def _render_slots_label(self):
        """A slot-címke — EGY igazságforrás (a ▼/▲ állítás és a periodikus MT5
        frissítés is ezt hívja).

        A szabad slotok mellett kiírja a nyitott pozíciók BONTÁSÁT is, mert csak
        a NEM kockázatmentes pozíciók foglalnak slotot (core.risk_manager.
        SlotManager.occupied) — így max 4 slot mellett is lehet szabályosan 8
        nyitott pozíció, ha 4 már biztosított. A bontás nélkül ez bugnak látszik.
        A ráépített lábak eleve kockázatmentesek (az SL az átlagáron), ezért is
        nőhet a darabszám a kockázat növekedése nélkül."""
        free = self._free_slots
        txt  = f"Szabad slotok: {free}/{self._max_slots}"
        if self._open_total:
            rf = self._open_total - self._open_occupied
            txt += (f"  ·  nyitva {self._open_total} "
                    f"({self._open_occupied} kockázatos"
                    + (f" + {rf} biztosított)" if rf else ")"))
        self.lbl_slots.config(text=txt, fg=FG_GREEN if free > 0 else FG_RED)

    def _change_slots(self, delta: int):
        """Max slotszám növelése/csökkentése a felületről.
        Csökkenteni csak a jelenleg FOGLALT (nyitott) slotok számáig lehet —
        egy nyitott pozíciót sosem 'zárunk ki' a limit alá szorítással."""
        occupied = max(0, self._max_slots - self._free_slots)
        new_max  = self._max_slots + delta
        if new_max < 1 or new_max < occupied:
            return
        self._max_slots  = new_max
        self._free_slots = max(0, new_max - occupied)
        # A motor SlotManager-ének frissítése (élő módban)
        if self._on_slots_change:
            try:
                self._on_slots_change(new_max)
            except Exception:
                pass
        # Perzisztálás a config.json-ba (csak a váz-szekciók)
        self.cfg["trading"]["max_open_slots"] = new_max
        self._save_main_config()
        self._render_slots_label()

    def _change_daily_limit(self, delta: int):
        """A napi veszteség-limit állítása a felületről (10$-os lépés, min. 10$).
        Az abszolút $ értéket a config trading.daily_loss_limit_usd kulcsa tárolja;
        első állításkor a jelenlegi effektív (pct-alapú) limitből indulunk. A live
        motor ugyanezt a cfg-dictet olvassa → a következő ciklusban már él."""
        from trading.backtest import daily_limit_usd as _dlim
        cur = _dlim(self.cfg["trading"], self._balance)
        # 10$-ra kerekített kiindulás (a pct-ből származó érték tört lehet)
        new = max(10, int(round(cur / 10.0)) * 10 + delta)
        self.cfg["trading"]["daily_loss_limit_usd"] = float(new)
        self._save_main_config()
        # Azonnali kijelzés-frissítés (a periodikus update is felülírja majd)
        total_daily = sum(ds.daily_pnl for ds in self.dashboard_ref.values())
        hit = total_daily <= -new
        self.lbl_limit.config(
            text=(f"Napi limit: STOP  ({total_daily:+.0f}$ / -{new}$)" if hit
                  else f"Napi limit: {total_daily:+.0f}$ / -{new}$"),
            fg=FG_RED if hit else FG_GREEN)

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
            all_syms = [s for s in self.dashboard_ref
                        if isinstance(self.cfg["pairs"].get(s), dict)]
            # 1) Olcsó ár-frissítés MINDEN párra, MINDIG — optimalizálás alatt IS!
            #    Ez tartja naprakészen a BID/ASK/Vált.%/Spread-et (a live kereskedéshez
            #    kell). Biztonságos: az optimizer NEM nyúl MT5-höz (parquet/Optuna), a
            #    hívás MT5_LOCK alatt fut, mint a live_trader (ami szintén megy közben).
            #    KORÁBBI HIBA: az egész loop az opt-kapu alá volt zárva → opt közben
            #    eltűnt a BID/ASK minden páron, a live DJ30-on is.
            for sym in all_syms:
                try:
                    self._refresh_price(sym)
                    self._refresh_light_extras(sym)   # napi nyitóár + max spread (opt közben is)
                except Exception:
                    pass
            # 2) Drága indikátor-számítás: CSAK ha nincs optimizer (CPU-kímélés + az
            #    indikátor-út bar-letöltést is végez). Minden pár ritkán, LIVE gyakrabban.
            if not self._opt_ctrl._running:
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
            ds.point      = info.point
        ref = ds.bid if ds.bid is not None else ds.ask
        if ref is not None and ds.day_open:
            ds.change_pct = (ref - ds.day_open) / ds.day_open * 100.0

    def _light_pair_data(self, symbol: str) -> dict:
        """Az optimalizált JSON TELJES tartalma (params + test_summary) az extra-frissítő-
        höz: a params az ATR/spread-számításhoz, a test_summary a Minőség-grade-hez.
        Throttle-olva hívjuk, ezért a JSON-olvasás elenyésző. Üres dict, ha nincs/hiba."""
        try:
            # FONTOS: lokális import — modul-szinten nincs params_file; enélkül
            # NameError keletkezett, amit a except lenyelt → a Minőség-grade
            # optimalizálás alatt SOSEM jelent meg (v1.30.3 regresszió).
            from core.params_store import params_file
            pf = params_file(symbol, self.strategy.name)
            if pf.exists():
                return json.load(open(pf, encoding="utf-8")) or {}
        except Exception:
            pass
        return {}

    def _refresh_light_extras(self, symbol: str):
        """Olcsó 'extrák', amikhez PÁR GYERTYA kell (nem tick): NAPI NYITÓÁR (Vált.%-hoz)
        + MAX SPREAD (ATR-alapú, a Spread 'kereskedhető?' zöld/piros jelzéséhez). A DRÁGA
        indikátor-úttól FÜGGETLENÜL fut — így optimalizálás alatt sem tűnik el a Vált.%
        és a kereskedhető-spread. Ritkán (throttle), mert lassan változnak."""
        ds = self.dashboard_ref.get(symbol)
        if ds is None:
            return
        import time as _t
        if _t.time() - getattr(ds, "_extras_ts", 0.0) < 15.0:
            return
        ds._extras_ts = _t.time()
        try:
            import MetaTrader5 as _mt5
            import pandas as _pd
            from core.mt5_connector import MT5_LOCK
            from core.indicator_engine import atr as _atr
        except Exception:
            return
        data = self._light_pair_data(symbol)
        prm = data.get("params") or {}
        if not prm:
            try:
                prm = self.strategy.base_params(self.cfg)
            except Exception:
                prm = {}
        atr_period = int(prm.get("atr_period", 14))
        # Minőség (grade) a test_summary-ből — OLCSÓ (nincs gyertya) → opt közben is.
        _ts = data.get("test_summary") or {}
        if _ts:
            try:
                _gt, _gc, _gr = self.strategy.grade(_ts, self.cfg)
                ds.opt_grade = (_gt, _gc)
                ds.opt_grade_reason = _gr
            except Exception:
                pass
        # ~300 M15 gyertya: fedi az ATR-t (max spread) ÉS a regime-osztályozó warmupját
        # (atr_avg_period=100) a Piac oszlophoz. Egyetlen copy_rates hívás → olcsó.
        with MT5_LOCK:
            _mt5.symbol_select(symbol, True)
            d1  = _mt5.copy_rates_from_pos(symbol, _mt5.TIMEFRAME_D1, 0, 1)
            m15 = _mt5.copy_rates_from_pos(symbol, _mt5.TIMEFRAME_M15, 0, 300)
        # Napi nyitóár → Vált.% (a legfrissebb bid-del arányosítva)
        if d1 is not None and len(d1):
            ds.day_open = float(d1[-1]["open"])
            ref = ds.bid if ds.bid is not None else ds.ask
            if ref is not None and ds.day_open:
                ds.change_pct = (ref - ds.day_open) / ds.day_open * 100.0
        point = getattr(ds, "point", None)
        _df15 = _pd.DataFrame(m15) if (m15 is not None and len(m15) > 2) else None
        # Max spread (ATR × ratio, min-padlóval) → a Spread cella zöld/piros jelzése
        if _df15 is not None and point:
            try:
                atr_val = _atr(_df15["high"], _df15["low"], _df15["close"], atr_period).iloc[-2]
                if atr_val == atr_val:   # not NaN
                    atr_pts  = int(atr_val / point)
                    ratio    = float(prm.get("max_spread_atr_ratio", 0.20))
                    pair_pip = float(self.cfg["pairs"].get(symbol, {}).get(
                                     "pip_size", point * 10))
                    pip_to_pt = max(1, round(pair_pip / point))
                    min_pts  = max(1, int(float(prm.get("min_spread_pips", 2.0)) * pip_to_pt))
                    ds.max_spread_pts = max(min_pts, int(atr_pts * ratio))
            except Exception:
                pass
        # Piac (regime) állapot → a Piac oszlop — opt közben is (a drága út opt-kapuzott)
        if _df15 is not None:
            try:
                from core import market_strategy as _ms
                _msname = _ms.market_name_of(self.cfg.get("pairs", {}).get(symbol, {}) or {})
                ds.market_strategy = _msname
                if _msname:
                    _cat = _ms.latest_category(_msname, _df15)
                    if _cat:
                        ds.market_state_label, ds.market_state_color = _ms.display(_cat)
            except Exception:
                pass
        # TF-együttállás (M1/M5/M15 SMA-irány) → az „Együtt" oszlop. Idősíkonként
        # NATIVE copy_rates (nincs resample-torzítás); sign(close − SMA(n)).
        try:
            from core import tf_align as _tfa
            from core import mt5_connector as _mc
            _tfa_en, _tfa_tfs, _tfa_sma, _ = _tfa.config_for(self.cfg, symbol)
            if _tfa_en:
                _closes = _mc.tf_closes(symbol, _tfa_tfs, _tfa_sma + 5)
                ds.tf_align_dir, ds.tf_align_signs = _tfa.alignment(
                    _closes, _tfa_tfs, _tfa_sma)
                ds.tf_align_labels = _tfa.labels(_tfa_tfs)
            else:
                ds.tf_align_signs, ds.tf_align_dir = [], None
        except Exception:
            pass
        # 'Utolsó opt' PERZISZTENS címke (a done-marker idejéből) — ha nem épp optimalizál.
        # Így restart / opt közben sem tűnik el (nem az in-memory 'Kész ✓'-ra hagyatkozik).
        if self.instrument_state.get(symbol) not in ("OPTIMIZING", "QUEUED"):
            _cur = self.optimizer_status.get(symbol, "")
            if (not _cur or "Kész" in _cur or "Utolsó opt" in _cur
                    or _cur.startswith("Opt:")):
                _lbl = self._opt_done_label(symbol)
                if _lbl:
                    self.optimizer_status[symbol] = _lbl

    def _opt_done_label(self, symbol: str) -> str:
        """'Utolsó opt' címke a perzisztens frissítéshez. Egy stratégia esetén a
        klasszikus 'Utolsó opt: yy/mm/dd'. TÖBB stratégia esetén PER-STRATÉGIA
        bontás ('Opt: wpr_sma 07/16 · ml_ai —'), hogy látsszon MELYIK stratégia
        MIKOR frissült (a '—' a még nem optimalizáltat jelöli)."""
        try:
            from strategy import enabled_strategy_names
            names = enabled_strategy_names(self.cfg, symbol)
        except Exception:
            names = [self.strategy.name]
        if not names:
            return ""
        if len(names) == 1:
            return opt_done_label(symbol, names[0])
        parts = []
        for n in names:
            d = opt_done_date(symbol, n)
            parts.append(f"{n} {d.strftime('%m/%d')}" if d else f"{n} —")
        return "Opt: " + " · ".join(parts)

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
        from core.params_store import params_file

        ds = self.dashboard_ref.get(symbol)
        if ds is None or not isinstance(self.cfg["pairs"].get(symbol), dict):
            return

        # Paraméterek: optimalizált, ha van; egyébként alap.
        params_f = params_file(symbol)
        if params_f.exists():
            with open(params_f, encoding="utf-8") as f:
                data = json.load(f)
            params = data.get("params", {})
            ds.trained = True
            # Minősítés a test_summary (out-of-sample) alapján — a stratégián át
            txt, col, reason = self.strategy.grade(data.get("test_summary", {}), self.cfg)
            ds.opt_grade = (txt, col)
            ds.opt_grade_reason = reason
            # Külsőleg (más app által) optimalizált párt is "vegyük észre":
            # ha nem épp most optimalizál, a perzisztens 'Utolsó opt: <dátum>' címke.
            if self.instrument_state.get(symbol) not in ("OPTIMIZING", "QUEUED"):
                self.optimizer_status[symbol] = self._opt_done_label(symbol) or "Kész ✓"
        else:
            params = self.strategy.base_params(self.cfg)
            ds.trained = False
            ds.opt_grade = None
            ds.opt_grade_reason = ""
            if not params.get("sma_period"):
                return

        timeframes = self.strategy.timeframes()

        primary = timeframes[0].label  # a "fő" időkeret (ATR-hez)

        with MT5_LOCK:
            _mt5.symbol_select(symbol, True)   # streameljen akkor is, ha letiltott
            raw_bars = {}
            for tf in timeframes:
                # MÉLY ablak (signal_warmup_bars) — hogy a compute_display jelzés-
                # állapota EGYEZZEN a vizzel és a motorral (a sekély warmup nem látná
                # a régi „jó zóna"-élesítést → a kör tévesen szürke maradna).
                warmup = self.strategy.signal_warmup_bars(params, tf.label)
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

        # No-trade órák → a compute_display jelzés-visszajátszása ugyanúgy RESETEL a
        # szüneteknél, mint a live motor és a viz (a kör a szünet után nulláról).
        from core.params_store import resolve_trade_hours as _rth
        _th = _rth(symbol, self.strategy.name,
                   (self.cfg.get("pairs", {}).get(symbol, {}) or {}).get("trade_hours"))
        _nt = (set(range(24)) - {int(h) for h in _th}) if _th is not None else set()
        md = MarketData(symbol=symbol, params=params, bars=bars, no_trade_hours=_nt)
        # Az instrumentumon ENGEDÉLYEZETT stratégiák (a GUI szürkíti a kikapcsoltakat).
        from strategy import enabled_strategy_names, get_strategy_by_name
        enabled = enabled_strategy_names(self.cfg, symbol)
        ds.enabled_strategies = enabled
        # Piac-előszűrő AKTUÁLIS állapota a „Piac" oszlophoz (ha van kiválasztva).
        from core import market_strategy as _ms
        _pcfg = self.cfg.get("pairs", {}).get(symbol, {}) or {}
        ds.market_strategy = _ms.market_name_of(_pcfg)
        if ds.market_strategy and bars.get("M15") is not None:
            try:
                _cat = _ms.latest_category(ds.market_strategy, bars["M15"])
                if _cat:
                    ds.market_state_label, ds.market_state_color = _ms.display(_cat)
            except Exception:
                pass
        # Ha a MOTOR (LIVE pár) frissen írta a jelzés-cellákat a saját állapotából,
        # NE írjuk felül a rekonstrukcióval — a motor az egyetlen forrás. Ha a motor
        # rég nem frissített (STOPPED / session-en kívül / demo), a GUI rekonstruál
        # STRATÉGIÁNKÉNT, mindegyik a SAJÁT paramétereivel (per-stratégia cellák).
        if time.time() - getattr(ds, "cells_ts", 0.0) >= 30.0:
            for sn in enabled:
                try:
                    st = get_strategy_by_name(sn)
                    if sn == self.strategy.name:
                        sp = params                    # már betöltve fent
                    else:
                        _f = params_file(symbol, sn)
                        if _f.exists():
                            sp = json.load(open(_f, encoding="utf-8")).get("params", {})
                        else:
                            # A stratégia SAJÁT config-nézetéből (a cfg a primary
                            # szekcióival van merge-elve — az nem az övé).
                            from strategy.settings import config_for_strategy
                            sp = st.base_params(config_for_strategy(self.cfg, sn))
                    # Pár-azonosító injektálás (mint a motoroknál): pl. az ml_ai
                    # feature-számítása/modell-betöltése igényli.
                    sp = {**sp, "symbol": symbol,
                          "pip_size": _pcfg.get("pip_size", 0.0001)}
                    sp.setdefault("sess_start", _pcfg.get("sess_start", 0))
                    sp.setdefault("sess_end",   _pcfg.get("sess_end", 24))
                    smd = MarketData(symbol=symbol, params=sp, bars=bars)
                    cells = st.compute_display(smd)
                    ds.strategy_cells[sn] = {k: (c.text, c.color)
                                             for k, c in cells.items()}
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
                        atr_pts  = int(atr_val / info.point)
                        ratio    = params.get("max_spread_atr_ratio", 0.20)
                        pair_pip = float(self.cfg["pairs"].get(symbol, {}).get(
                                         "pip_size", info.point * 10))
                        pip_to_pt = max(1, round(pair_pip / info.point))
                        min_pts  = max(1, int(params.get("min_spread_pips", 2.0)
                                              * pip_to_pt))
                        ds.max_spread_pts = max(min_pts, int(atr_pts * ratio))
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
                        open_positions_by_symbol, open_positions_detailed,
                        closed_positions_today, server_offset_sec)
                    info = connection_info(self.cfg)
                    self._mt5_cache["connected"] = info.get("connected", False)
                    self._mt5_cache["info"]      = info
                    if info.get("connected"):
                        self._mt5_cache["daily_pnl"] = _dpnl()
                        self._mt5_cache["positions"] = open_positions_by_symbol()
                        self._mt5_cache["positions_detail"] = open_positions_detailed()
                        self._mt5_cache["closed_today"] = closed_positions_today()
                        # Bróker-idő eltolás (a trade_hours/chart szerver-idejéhez)
                        _off = server_offset_sec(list(self.cfg.get("pairs", {}).keys()))
                        if _off is not None:
                            self._mt5_cache["server_offset_sec"] = _off
                    else:
                        self._mt5_cache["daily_pnl"] = None
                        self._mt5_cache["positions"] = {}
                        self._mt5_cache["positions_detail"] = []
                        self._mt5_cache["closed_today"] = []
                except Exception:
                    pass
                _t.sleep(5)
        threading.Thread(target=_loop, daemon=True, name="MT5BgPoller").start()

    def _is_no_trade_now(self, symbol: str) -> bool:
        """A jelenlegi BRÓKER-óra kimarad-e az aktív stratégia trade_hours-ából
        erre a párra? (A live óra-kapujával azonos logika — szerver/chart idő.)
        A jelölés STRATÉGIA-hatókörű: az ELSŐDLEGES (első engedélyezett) stratégia
        óra-fájlját nézi (`{symbol}_hours.json`), visszaesve a config.json legacy
        `trade_hours`-ra — így ugyanaz, amivel a live óra-kapuja számol."""
        off = getattr(self, "_mt5_cache", {}).get("server_offset_sec")
        if off is None:
            return False   # nincs bróker-idő → ne jelezzünk félre
        pc = self.cfg.get("pairs", {}).get(symbol, {})
        if not isinstance(pc, dict):
            return False
        bh = (datetime.now(timezone.utc) + timedelta(seconds=off)).hour
        from core.params_store import resolve_trade_hours
        from strategy import enabled_strategy_names
        _names = enabled_strategy_names(self.cfg, symbol)
        _sn = _names[0] if _names else None
        th = resolve_trade_hours(symbol, _sn, pc.get("trade_hours"))
        if th is not None:
            return bh not in {int(h) for h in th}
        # Visszafelé kompatibilis: sess_start/sess_end tartomány (mint a live).
        return not (pc.get("sess_start", 0) <= bh < pc.get("sess_end", 24))

    # ── Fő frissítés (1 mp, csak Python — nem blokkol MT5-re) ────────────
    def _refresh(self):
        now = datetime.now(timezone.utc)
        # A felső óra a BRÓKER-időt mutatja (a trade_hours/óra-kapu és a chart is
        # ezen jár), az UTC-t másodlagosként. Így nincs félreértés a no-trade
        # órákkal. Ha nincs kapcsolat/offset, csak UTC látszik.
        _off = getattr(self, "_mt5_cache", {}).get("server_offset_sec")
        if _off is not None:
            bt = now + timedelta(seconds=_off)
            self.lbl_time.config(text=f"Bróker {bt:%H:%M:%S}  ·  UTC {now:%H:%M}")
        else:
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
            from core import rr_state as _rrs0
            _rrs0.load()                      # induló per-pár preset állapot
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

        # Per-instrumentum NAPI P&L a MAI lezárt trade-ekből (MT5 history — HITELES,
        # újraindítás-biztos). Korábban a state.daily_pnl session-local volt: bot-
        # újraindítás után a korábbi zárt trade-eket "elfelejtette", és egy páron a
        # napi P&L csak az UTOLSÓ zárt trade-et mutatta (nem az összeg). Így most a
        # "Lezárt (ma)" füllel és a felső összesítővel is egyezik. Csak kapcsolódva
        # írjuk felül (offline a closed_today [] fallback → nem hiteles).
        if connected:
            daily_by_symbol: dict = {}
            for c in (cache.get("closed_today") or []):
                s = c.get("symbol")
                if s is not None:
                    daily_by_symbol[s] = daily_by_symbol.get(s, 0.0) + c.get("pnl", 0.0)
            for _sym, _ds in self.dashboard_ref.items():
                if _ds is not None:
                    _ds.daily_pnl = daily_by_symbol.get(_sym, 0.0)

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
        # A limit értéke EGY igazságforrásból (mint a live kapué): abszolút $
        # (daily_loss_limit_usd, a ▼/▲ állítja), különben pct × egyenleg.
        from trading.backtest import daily_limit_usd as _dlim
        _limit = _dlim(self.cfg["trading"], self._balance)
        limit_hit = (_limit > 0 and total_daily <= -_limit)
        self.lbl_limit.config(
            text=(f"Napi limit: STOP  ({total_daily:+.0f}$ / -{_limit:.0f}$)" if limit_hit
                  else f"Napi limit: {total_daily:+.0f}$ / -{_limit:.0f}$"),
            fg=FG_RED if limit_hit else FG_GREEN)

        if mt5_positions is not None:
            # Csak a NEM kockázatmentes pozíciók foglalnak slotot (a kockázatmentes
            # felszabadítja) — egyezik a motor SlotManager-ének logikájával.
            occupied = sum(p.get("occupied", p.get("count", 1))
                           for p in mt5_positions.values())
            self._free_slots = max(0, self._max_slots - occupied)
            # A bontáshoz az ÖSSZES nyitott darab is kell (a `count` a
            # kockázatmenteseket is tartalmazza) — lásd `_render_slots_label`.
            self._open_total    = sum(p.get("count", 1) for p in mt5_positions.values())
            self._open_occupied = occupied
            self._render_slots_label()

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
                    from core import rr_state as _rrs
                    ds.rr_preset = _rrs.effective_preset(symbol)
                    ds.risky = (ds.rr_preset == "risky")
                    no_trade = (inst_state == "LIVE" and self._is_no_trade_now(symbol))
                    row.update(ds, inst_state, opt_status,
                               connected=getattr(self, "_connected", False),
                               no_trade=no_trade)
                if inst_state == "LIVE":
                    live_count += 1

        if hasattr(self, "lbl_status"):
            # "Utolsó frissítés" = lokális UI-esemény → HELYI idő (nem UTC/bróker).
            local_now = datetime.now().strftime("%H:%M:%S")
            self.lbl_status.config(
                text=f"Utolsó frissítés: {local_now}  |  LIVE: {live_count}")

        # Pozíciók fül frissítése
        if hasattr(self, "_pos_tab"):
            try:
                self._pos_tab.refresh()
            except Exception:
                pass

        # Lezárt (ma) fül frissítése
        if hasattr(self, "_closed_tab"):
            try:
                self._closed_tab.refresh()
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
        # Auto-folytatás: a megszakadt optimalizálások újraindítása INDÍTÁSKOR.
        # Késleltetve, hogy a live_trader induló LIVE-jelölése (magic-recovery +
        # run_state) már beálljon → a kereskedő szimbólumokat NE optimalizáljuk.
        if self._auto_resume_opt:
            self.root.after(4000, self._resume_optimizations)
        try:
            self.root.mainloop()
        finally:
            try:
                self._opt_ctrl.shutdown()
            except Exception:
                pass

    def _resume_optimizations(self):
        """A befejezetlen study-k sorba állítása (háttérszálon, hogy az UI ne akadjon)."""
        threading.Thread(target=self._opt_ctrl.resume_unfinished, daemon=True,
                         name="OptResume").start()


# ---------------------------------------------------------------------------
# Demo mód
# ---------------------------------------------------------------------------

def _demo_dashboard(cfg: dict):
    """Demo: UI layout + state machine bemutatása MT5 nélkül.
    A stratégia-cellákat szimulált értékekkel tölti, hogy az oszlopok lássanak."""
    import random
    from trading.live_trader import PairDashboardState

    strategy   = get_strategy(cfg)
    from core.params_store import set_active_strategy, strategy_dir
    set_active_strategy(strategy.name)
    params_dir = strategy_dir(strategy.name)
    real_trained = {f.stem for f in params_dir.glob("*.json")} if params_dir.exists() else set()
    symbols = [s for s, p in cfg["pairs"].items() if isinstance(p, dict)]

    states_pool = ["LIVE"] * 4 + ["STOPPED"] * 6
    random.shuffle(states_pool)

    db, inst_state, opt_status = {}, {}, {}
    from strategy import (available_strategy_names, get_strategy_by_name,
                          enabled_strategy_names as _enabled_names)
    reg_strats = [get_strategy_by_name(n) for n in available_strategy_names(cfg)]
    # Per-stratégia stádium-kulcsok a demó köreihez {strat_név: [stádium_kulcs,…]}
    stages_by_strat = {st.name: [sk for c in st.columns() if c.kind == "marker"
                                 for sk, _ in c.stages] for st in reg_strats}

    for i, symbol in enumerate(symbols):
        trained = symbol in real_trained
        st      = states_pool[i % len(states_pool)] if trained else "STOPPED"
        inst_state[symbol] = st
        opt_status[symbol] = "Kész ✓" if trained else ""

        # Valós minősítés a test_summary alapján (ha optimalizált)
        grade_cell, grade_reason = None, ""
        if trained:
            try:
                _data = json.load(open(params_dir / f"{symbol}.json", encoding="utf-8"))
                gtxt, gcol, greason = strategy.grade(_data.get("test_summary", {}), cfg)
                grade_cell, grade_reason = (gtxt, gcol), greason
            except Exception:
                pass

        base = round(random.uniform(0.9, 1.6), 5)
        ds = PairDashboardState(
            symbol=symbol, enabled=trained, trained=trained,
            bid=base, ask=round(base + 0.0002, 5), prev_bid=base, prev_ask=base,
            digits=5, day_open=round(base * random.uniform(0.99, 1.01), 5),
            change_pct=round(random.uniform(-0.6, 0.6), 2),
            spread_pts=random.randint(6, 18), max_spread_pts=random.randint(12, 25),
            position_pnl=None, risk_free=False, daily_pnl=0.0,
            opt_grade=grade_cell, opt_grade_reason=grade_reason,
            viz_enabled=cfg["pairs"][symbol].get("viz_enabled", True),
            show_trades=cfg["pairs"][symbol].get("show_trades", True),
            # Per-pár engedélyezett stratégiák (mint az éles induláskor) — a
            # jelölő-oszlop a nem engedélyezettet apró ponttal különbözteti meg.
            enabled_strategies=_enabled_names(cfg, symbol),
        )
        # Stratégia-cellák szimulálása per stratégia (csak LIVE pároknál)
        if st == "LIVE":
            for sname, sks in stages_by_strat.items():
                ds.strategy_cells[sname] = {
                    sk: ("●", random.choice(["green", "red", "muted", "muted"]))
                    for sk in sks}
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
