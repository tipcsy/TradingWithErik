"""Pozícióépítés (ráépítés) — CSV-export a backtest eredményéből.

A Backtest-ablak összesítve mutatja az építés hozadékát („N ráépítés M kötésen,
+x R / +y $"); ez a modul a MÖGÖTTES SOROKAT írja ki, hogy Excelben tételesen
átnézhető legyen (mint az optimalizálás Trials CSV-je):

  • egy sor = egy ÉPÍTETT kötés (csomag), a lábak (legs) bontásával,
  • `adalek_pnl_usd` / `adalek_r` = KIZÁRÓLAG a ráépített lábak eredménye a
    záróáron (az R az induló láb kockázatához, `risk_usd`-hez mérve),
  • `alap_pnl_usd` = az induló láb (esetleges részleges zárás után maradt) része,
  • `labak` = az add-onok árai és méretei, hogy a piramis is látszódjon.

Formátum: `;` elválasztó + `,` tizedes + UTF-8 BOM — a magyar Excel így natívan
nyitja (ugyanaz a konvenció, mint a `*_trials.csv`).
"""

from __future__ import annotations

import csv
from pathlib import Path

import pandas as pd

HEADER = ["nyitas", "zaras", "irany", "statusz", "labak_db", "alap_lot",
          "adalek_lot", "osszes_lot", "belepo", "atlagar", "zaro_ar",
          "alap_pnl_usd", "adalek_pnl_usd", "adalek_r", "trade_pnl_usd",
          "kockazat_usd", "adalek_labak"]


def _hu(x, nd=2) -> str:
    """Szám → magyar tizedes (a `;` elválasztó mellé)."""
    if x is None:
        return ""
    return f"{float(x):.{nd}f}".replace(".", ",")


def rows_from_result(result) -> list[list]:
    """Az ÉPÍTETT (több lábú) LEZÁRT kötések sorai. Üres lista, ha nem volt építés."""
    rows: list[list] = []
    for t in getattr(result, "trades", []):
        legs = getattr(t, "legs", None) or []
        if len(legs) < 2 or t.close_price is None or t.status == "open":
            continue
        base_price, base_lot = legs[0]
        add_lot = sum(l for _, l in legs[1:])

        def _leg_pnl(price, lot):
            diff = t.close_price - price
            if t.direction == "SELL":
                diff = -diff
            return (diff / t.pip_size) * lot * t.pv1_usd

        base_pnl = _leg_pnl(base_price, base_lot)
        add_pnl  = sum(_leg_pnl(p, l) for p, l in legs[1:])
        avg = (sum(p * l for p, l in legs) / sum(l for _, l in legs)) if legs else 0.0
        rows.append([
            pd.Timestamp(t.open_time).strftime("%Y-%m-%d %H:%M"),
            pd.Timestamp(t.close_time).strftime("%Y-%m-%d %H:%M") if t.close_time is not None else "",
            t.direction, t.status, len(legs),
            _hu(base_lot), _hu(add_lot), _hu(sum(l for _, l in legs)),
            _hu(base_price, 5), _hu(avg, 5), _hu(t.close_price, 5),
            _hu(base_pnl), _hu(add_pnl),
            _hu(add_pnl / t.risk_usd if t.risk_usd else 0.0),
            _hu(t.pnl_usd), _hu(t.risk_usd),
            " | ".join(f"{_hu(p, 5)}×{_hu(l)}" for p, l in legs[1:]),
        ])
    return rows


def export_build_csv(result, symbol: str, out_dir) -> Path | None:
    """A ráépítés-sorok kiírása `build_<symbol>_<ts>.csv` néven.
    Visszaad: a fájl útvonala, vagy None, ha nem volt egyetlen ráépítés sem."""
    rows = rows_from_result(result)
    if not rows:
        return None
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = pd.Timestamp.now().strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"build_{symbol}_{ts}.csv"
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(HEADER)
        w.writerows(rows)
    return path
