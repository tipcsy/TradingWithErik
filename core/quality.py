"""
Optimalizált instrumentum minősítése a test_summary (out-of-sample) alapján.

Szabály-alapú, "legrosszabb-elv" besorolás — átlátható, mert megmondja, MI
húzza le a párt (indok). A küszöbök a config.json "quality" blokkjából
felülírhatók. A modul stratégia- és tkinter-független (szemantikus szín-neveket
ad vissza: "green"/"yellow"/"orange"/"red"/"muted").

Fő mérőszám a profit_factor (PF), mert a win_rate önmagában félrevezető:
2:1 hozam/kockázatnál a nullszaldó ~33% win_rate.
"""

from typing import Optional

# Alapértelmezett küszöbök (a config "quality" blokkja felülírja)
_DEFAULTS = {
    "min_trades":   15,
    "maxdd_mid":    0.18,
    "maxdd_weak":   0.25,
    "maxdd_bad":    0.35,
    "pf_mid":       1.4,
    "pf_weak":      1.2,
    "pf_bad":       1.0,
    "winrate_weak": 0.35,
    "winrate_good": 0.45,
}


def _q(cfg: dict) -> dict:
    d = dict(_DEFAULTS)
    d.update((cfg or {}).get("quality", {}) or {})
    return d


def grade(test_summary: dict, cfg: dict) -> tuple[str, str, str]:
    """(minősítő_szöveg, szín-név, indok) a test_summary alapján.

    Ha nincs adat → ("—", "muted", "").
    """
    if not test_summary:
        return ("—", "muted", "")
    q = _q(cfg)
    trades = test_summary.get("trades", 0)
    pnl    = test_summary.get("total_pnl", 0.0)
    pf     = test_summary.get("profit_factor", 0.0)
    wr     = test_summary.get("win_rate", 0.0)
    mdd    = test_summary.get("max_drawdown", 1.0)

    # 🔴 Rossz — bármelyik súlyos kizáró feltétel
    if pnl <= 0:
        return ("Rossz", "red", "veszteséges")
    if trades < q["min_trades"]:
        return ("Rossz", "red", f"kevés trade ({trades})")
    if pf < q["pf_bad"]:
        return ("Rossz", "red", f"PF {pf:.2f}")
    if mdd >= q["maxdd_bad"]:
        return ("Rossz", "red", f"MaxDD {mdd*100:.0f}%")

    # 🟠 Gyenge
    if mdd >= q["maxdd_weak"]:
        return ("Gyenge", "orange", f"MaxDD {mdd*100:.0f}%")
    if pf < q["pf_weak"]:
        return ("Gyenge", "orange", f"PF {pf:.2f}")

    # 🟡 Közepes  (a win_rate csak enyhe jelzés: a PF már fedi a nyereségességet,
    #  alacsony WR + erős PF = ritka nagy nyerő, attól még jó lehet)
    if mdd >= q["maxdd_mid"]:
        return ("Közepes", "yellow", f"MaxDD {mdd*100:.0f}%")
    if pf < q["pf_mid"]:
        return ("Közepes", "yellow", f"PF {pf:.2f}")
    if wr < q["winrate_weak"]:
        return ("Közepes", "yellow", f"Win {wr*100:.0f}%")

    # 🟢 Jó
    return ("Jó", "green", "")


_RANK = {"Jó": 0, "Közepes": 1, "Gyenge": 2, "Rossz": 3}


def grade_rank(grade_text: str) -> int:
    """Minősítő szöveg → rang (kisebb = erősebb). Ismeretlen/nincs → 4.
    A 'Csak erősebb' korreláció-mód a feldolgozási sorrendhez használja."""
    return _RANK.get(grade_text, 4)


def metric_colors(test_summary: dict, cfg: dict) -> dict:
    """Per-metrika szemantikus szín (a részletes popuphoz)."""
    if not test_summary:
        return {}
    q = _q(cfg)
    pnl = test_summary.get("total_pnl", 0.0)
    pf  = test_summary.get("profit_factor", 0.0)
    wr  = test_summary.get("win_rate", 0.0)
    mdd = test_summary.get("max_drawdown", 1.0)

    def dd_c(v):
        return ("green" if v < q["maxdd_mid"] else "yellow" if v < q["maxdd_weak"]
                else "orange" if v < q["maxdd_bad"] else "red")

    def pf_c(v):
        return ("green" if v >= q["pf_mid"] else "yellow" if v >= q["pf_weak"]
                else "orange" if v >= q["pf_bad"] else "red")

    def wr_c(v):
        return ("green" if v >= q["winrate_good"] else "yellow" if v >= q["winrate_weak"]
                else "red")

    return {
        "total_pnl":     "green" if pnl > 0 else "red",
        "profit_factor": pf_c(pf),
        "win_rate":      wr_c(wr),
        "max_drawdown":  dd_c(mdd),
    }
