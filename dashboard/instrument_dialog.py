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
import tkinter as tk
from datetime import datetime

from dashboard.theme import (
    BG, BG_HEADER,
    FG_WHITE, FG_GREEN, FG_RED, FG_GRAY, FG_GRAY_DIM,
    BTN_PLAY_BG, BTN_PLAY_FG, BTN_OPT_BG, BTN_BT_BG, BTN_BT_FG,
    BTN_DIS_BG, BTN_DIS_FG,
    color as sem_color,
)
from core.quality import metric_colors
from ml.optimizer import PARAMS_DIR

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

        self.pf = PARAMS_DIR / f"{symbol}.json"
        self.trials_csv = PARAMS_DIR / f"{symbol}_trials.csv"

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

        # A megjelenített/menthető paraméter-forrás: optimalizált, vagy alap.
        self._src   = dict(params) if params else default_params(cfg, strategy)
        self._keys  = sorted(k for k in self._src if not k.startswith("_"))
        # Típus-minta a mentéskori konverzióhoz (int/float/bool/str)
        self._types = {k: self._src[k] for k in self._keys}

        # ── trials CSV betöltése → {rank: {oszlop: nyers_str}} ──────────────
        self._rank_rows = self._load_trials()
        self._ranks = sorted(self._rank_rows)

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
        title = f"{self.symbol} — paraméterek"
        if self.is_new:
            title += " (új / kézi)"
        popup.title(title)
        popup.configure(bg=BG)
        popup.grab_set()

        ts = (self.data or {}).get("test_summary", {})

        # ── Fejléc: minősítés + metrikák, vagy „kézi" jelzés ────────────────
        if ts:
            gtxt, gcol, greason = self.strategy.grade(ts, self.cfg)
            mc = metric_colors(ts, self.cfg)
            hdr = tk.Frame(popup, bg=BG)
            hdr.pack(anchor="w", padx=10, pady=(10, 2))
            tk.Label(hdr, text=f"Minősítés: {gtxt}", bg=BG, fg=sem_color(gcol),
                     font=self._hf).pack(side="left")
            if greason:
                tk.Label(hdr, text=f"  ({greason})", bg=BG, fg=FG_GRAY,
                         font=self._sf).pack(side="left")

            metrics = tk.Frame(popup, bg=BG)
            metrics.pack(anchor="w", padx=10, pady=(0, 4))

            def _metric(label, value, color):
                cell = tk.Frame(metrics, bg=BG)
                cell.pack(side="left", padx=(0, 12))
                tk.Label(cell, text=label, bg=BG, fg=FG_GRAY,
                         font=self._sf).pack(side="left")
                tk.Label(cell, text=value, bg=BG, fg=sem_color(color),
                         font=self._sf).pack(side="left")
            _metric("Trade ", str(ts.get("trades", 0)), "white")
            _metric("P&L ", f"{ts.get('total_pnl',0):+.0f}$", mc.get("total_pnl", "white"))
            _metric("Win ", f"{ts.get('win_rate',0)*100:.0f}%", mc.get("win_rate", "white"))
            _metric("PF ", f"{ts.get('profit_factor',0):.2f}", mc.get("profit_factor", "white"))
            _metric("MaxDD ", f"{ts.get('max_drawdown',0)*100:.1f}%", mc.get("max_drawdown", "white"))
        else:
            tk.Label(popup, text=("Nincs optimalizált eredmény — kézi/alap paraméterek. "
                                  "A Mentés létrehozza a {}.json-t.".format(self.symbol)),
                     bg=BG, fg=FG_GRAY, font=self._sf, justify="left",
                     wraplength=560).pack(anchor="w", padx=10, pady=(10, 4))

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
            self.entries[k] = e

        self.lbl_err = tk.Label(popup, text="", bg=BG, fg=FG_RED, font=self._sf)
        self.lbl_err.pack(anchor="w", padx=10)

        # ── Gombsor ─────────────────────────────────────────────────────────
        btns = tk.Frame(popup, bg=BG)
        btns.pack(pady=10)
        tk.Button(btns, text="Mentés", bg=BTN_PLAY_BG, fg=BTN_PLAY_FG, relief="flat",
                  font=self._sf, command=self._save).pack(side="left", padx=6)
        if self._ranks:
            tk.Button(btns, text="Ment új sorszámként", bg=BTN_OPT_BG, fg="#ffffff",
                      relief="flat", font=self._sf,
                      command=self._save_as_new_rank).pack(side="left", padx=6)
        tk.Button(btns, text="Trials CSV", bg=BTN_BT_BG, fg=BTN_BT_FG, relief="flat",
                  font=self._sf, command=self._open_trials).pack(side="left", padx=6)
        tk.Button(btns, text="Mégse", bg=BTN_DIS_BG, fg=BTN_DIS_FG, relief="flat",
                  font=self._sf, command=popup.destroy).pack(side="left", padx=6)

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
        self._show_rank_metrics(rank, row)

    def _show_rank_metrics(self, rank: int, row: dict):
        summ = {
            "trades":        _num(row.get("trades")) or 0,
            "total_pnl":     _num(row.get("total_pnl")) or 0.0,
            "win_rate":      _num(row.get("win_rate")) or 0.0,
            "profit_factor": _num(row.get("profit_factor")) or 0.0,
            "max_drawdown":  _num(row.get("max_drawdown")) or 0.0,
        }
        note = (row.get("note") or "").strip()
        if summ["trades"] == 0 and not any(row.get(c) for c in
                                           ("win_rate", "total_pnl")):
            txt = f"#{rank}: nincs backtest-metrika" + (f" — {note}" if note else "")
            self.lbl_rank.config(text=txt, fg=FG_GRAY)
            return
        txt = (f"#{rank}  Win {summ['win_rate']*100:.0f}%   "
               f"MaxDD {summ['max_drawdown']*100:.1f}%   "
               f"P&L {summ['total_pnl']:+.0f}$   "
               f"Trade {int(summ['trades'])}   "
               f"PF {summ['profit_factor']:.2f}")
        if note:
            txt += f"   ({note})"
        self.lbl_rank.config(text=txt, fg=FG_WHITE)

    # ── Óra-rács (trade_hours) ──────────────────────────────────────────────
    def _build_hours(self, popup, ts):
        """A live óra-kapuja a config.json pairs.<SYM>.trade_hours listáját nézi.
        Az óránkénti P&L (az optimalizált test_summary-ből) segít eldönteni, mely
        órákat vegyük ki. Auto-javasol = a mínuszos órákat kiveszi. A config.json-
        ba ment (NEM az optimalizált JSON-ba)."""
        params = self._src
        hp_raw = (ts or {}).get("hourly_pnl", {})
        hourly = {}
        for _k, _v in hp_raw.items():
            try:
                hourly[int(_k)] = _v
            except (ValueError, TypeError):
                pass

        _pc = self.cfg.get("pairs", {}).get(self.symbol, {})
        _cur = _pc.get("trade_hours")
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

        hbtns = tk.Frame(popup, bg=BG)
        hbtns.pack(anchor="w", padx=10, pady=(0, 6))
        hlbl = tk.Label(hbtns, text="", bg=BG, fg=FG_GREEN, font=self._sf)

        def auto_suggest():
            for _h in range(24):
                _bb = hourly.get(_h)
                if _bb is not None:
                    hour_on[_h] = (_bb.get("pnl", 0.0) >= 0)
                    _paint(_h)
            hlbl.config(text="Javaslat betöltve — felülbírálható, majd Órák mentése.",
                        fg=FG_GRAY)

        def save_hours():
            sel = [h for h in range(24) if hour_on[h]]
            pcfg = self.cfg.setdefault("pairs", {}).setdefault(self.symbol, {})
            pcfg["trade_hours"] = sel
            try:
                self._save_main_config()
                hlbl.config(text=f"Mentve: {len(sel)} óra a config.json-ba.", fg=FG_GREEN)
            except Exception as ex:
                hlbl.config(text=f"Mentési hiba: {ex}", fg=FG_RED)

        tk.Button(hbtns, text="Auto-javasol", font=self._sf, bg=BTN_OPT_BG,
                  fg="#ffffff", relief="flat", command=auto_suggest).pack(side="left", padx=(0, 6))
        tk.Button(hbtns, text="Órák mentése", font=self._sf, bg=BTN_PLAY_BG,
                  fg="#1e1e2e", relief="flat", command=save_hours).pack(side="left", padx=(0, 6))
        hlbl.pack(side="left", padx=6)

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
        return True

    def _save(self):
        new_params = self._collect_params()
        if new_params is None:
            return
        if self._write_json(new_params):
            self.popup.destroy()

    def _save_as_new_rank(self):
        """A jelenlegi (kézzel átírt) paraméter-készlet mentése ÚJ sorszámként a
        trials CSV-be (501…) + a JSON-ba, hogy később visszatölthető legyen."""
        new_params = self._collect_params()
        if new_params is None:
            return
        new_rank = _MANUAL_RANK_BASE
        while new_rank in self._rank_rows:
            new_rank += 1
        try:
            self._append_manual_trial(new_rank, new_params)
        except Exception as ex:
            self.lbl_err.config(text=f"CSV-mentési hiba: {ex}")
            return
        if not self._write_json(new_params, extra={"manual_rank": new_rank}):
            return
        # In-memory frissítés → azonnal visszatölthető, a nyilak elérik.
        rec = {k: str(v) for k, v in new_params.items()}
        rec.update({"rank": str(new_rank), "note": "manual"})
        self._rank_rows[new_rank] = rec
        self._ranks = sorted(self._rank_rows)
        self.rank_var.set(str(new_rank))
        if self.lbl_rank is not None:
            self.lbl_rank.config(
                text=f"Elmentve új sorszámként: #{new_rank} (kézi).", fg=FG_GREEN)
        self.lbl_err.config(text="")

    def _append_manual_trial(self, rank: int, params: dict):
        """Egy kézi paraméter-sor hozzáfűzése a trials CSV-hez, `rank` oszloppal.

        pandas-szal olvassuk/írjuk vissza (magyar ';'+','), így ha a régi CSV-ben
        még nincs `rank` oszlop, most bekerül (a sor pozíciója szerint 1…N)."""
        import pandas as pd
        if self.trials_csv.exists():
            df = pd.read_csv(self.trials_csv, sep=";", decimal=",",
                             encoding="utf-8-sig")
        else:
            df = pd.DataFrame()
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
        df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
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
