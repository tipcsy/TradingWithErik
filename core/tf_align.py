"""
TF-együttállás figyelő — több-idősíkú SMA-irány (keretrendszer-szintű monitor).

Idősíkonként a trend-irány EGYSZERŰ: `sign(utolsó close − SMA(n))`. Ha a figyelt
idősíkok (alap: M1/M5/M15) MIND egy irányba mutatnak → erős együttállás ("S"):
BUY (mind fölfelé) vagy SELL (mind lefelé). Különben vegyes → nincs erős jel.

Ez egy MEGJELENÍTŐ (a dashboard „Együtt" oszlopa) — nem befolyásolja a kereskedést.
A modul SZÁNDÉKOSAN MT5-mentes és tiszta (tesztelhető): a bar-adatot (záróárak
idősíkonként) a hívó (dashboard) tölti native copy_rates-ből.

Konfiguráció (config.json, VÁZ-szint):
    "tf_align": { "enabled": true, "timeframes": [1, 5, 15], "sma_period": 50 }
"""

from __future__ import annotations

DEFAULT_TIMEFRAMES = [1, 5, 15]
DEFAULT_SMA = 50

# Idősík (perc) → rövid címke a cellához/tooltiphez.
TF_LABEL = {1: "M1", 5: "M5", 15: "M15", 30: "M30", 60: "H1", 240: "H4"}


def config(cfg: dict) -> tuple:
    """(enabled, timeframes, sma_period) a config.json `tf_align` szekciójából.
    Hiányzó kulcsok → az alapértékek (bekapcsolva, M1/M5/M15, SMA50)."""
    tc = (cfg.get("tf_align") or {})
    enabled = bool(tc.get("enabled", True))
    tfs = tc.get("timeframes") or DEFAULT_TIMEFRAMES
    try:
        tfs = [int(t) for t in tfs]
    except (TypeError, ValueError):
        tfs = list(DEFAULT_TIMEFRAMES)
    sma = int(tc.get("sma_period", DEFAULT_SMA))
    return enabled, tfs, max(2, sma)


def _sign(closes, n: int) -> int:
    """sign(utolsó close − SMA(n)) az adott idősík záróáraiból. 0, ha kevés adat
    vagy pont a SMA-n (semleges)."""
    if not closes or len(closes) < n:
        return 0
    tail = closes[-n:]
    sma = sum(tail) / n
    d = closes[-1] - sma
    return 1 if d > 0 else -1 if d < 0 else 0


def alignment(closes_by_tf: dict, timeframes: list, sma_period: int) -> tuple:
    """(direction, signs). `signs` a `timeframes` SORRENDJÉBEN (+1 fölfelé / −1
    lefelé / 0 semleges-vagy-adathiány). `direction` = 'BUY' ha MIND +1, 'SELL' ha
    MIND −1, különben None (vegyes/hiányos)."""
    signs = [_sign(closes_by_tf.get(tf), sma_period) for tf in timeframes]
    if signs and all(s == 1 for s in signs):
        direction = "BUY"
    elif signs and all(s == -1 for s in signs):
        direction = "SELL"
    else:
        direction = None
    return direction, signs


def labels(timeframes: list) -> list:
    """Az idősíkok rövid címkéi (a cella/tooltip sorrendjéhez)."""
    return [TF_LABEL.get(t, f"{t}m") for t in timeframes]
