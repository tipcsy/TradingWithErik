"""
MT5 vizualizáció — MINIMÁLIS PRÓBA (végpontig működő csővezeték).

Kiír EGYETLEN telített dobozt a `TradeForgeViz.mq5` indikátornak: a megadott
szimbólum utolsó ~20 M1 gyertyáját fedő zöld sávot a chart aljára.

Cél: bizonyítani a teljes csatornát (Python → Common\\Files fájl → MQL5
indikátor → chart-objektum). Mivel a doboz jobb sarka (t2) mindig a LEGUTOLSÓ
gyertyához igazodik, a szkript ÚJRAFUTTATÁSA MEGNÖVELI ugyanazt a dobozt
(stabil név → az indikátor ObjectMove-val módosítja, nem újat rajzol) — így az
UPSERT/„nő, nem törlődik" viselkedés is látszik.

Futtatás:  python tools/viz_test.py [SYMBOL]   (alapértelmezett: EURUSD)
"""

import logging
import sys
from pathlib import Path

import MetaTrader5 as mt5

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import mt5_connector, mt5_visual
from strategy.visual import Rect
from strategy.settings import load_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger(__name__)


def main():
    symbol = sys.argv[1] if len(sys.argv) > 1 else "EURUSD"

    cfg = load_config(ROOT / "config.json")
    if not mt5_connector.connect(cfg):
        log.error("MT5 kapcsolódás sikertelen.")
        sys.exit(1)

    try:
        with mt5_connector.MT5_LOCK:
            mt5.symbol_select(symbol, True)
            rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 0, 30)
        if rates is None or len(rates) < 20:
            log.error("%s — nincs elég M1 gyertya (%s).", symbol,
                      0 if rates is None else len(rates))
            sys.exit(1)

        # NYERS bar-idő integerek (nincs TZ-konverzió — pontosan a gyertyára esik).
        t1 = int(rates["time"][-20])
        t2 = int(rates["time"][-1])

        lows  = [float(x) for x in rates["low"][-20:]]
        highs = [float(x) for x in rates["high"][-20:]]
        lo, hi = min(lows), max(highs)
        band = (hi - lo) or (lo * 0.001)
        # A chart aljához közeli, jól látható sáv (a valódi SMA-doboz később a
        # chart aljához lesz pinnelve; a próbához explicit ársáv is elég).
        p1 = lo
        p2 = lo + band * 0.15

        box = Rect(name="test_box", t1=t1, p1=p1, t2=t2, p2=p2,
                   color="green", fill=True)
        path = mt5_visual.write(symbol, [box])

        if path is None:
            log.error("Nem sikerült a Common\\Files fájl írása.")
            sys.exit(1)
        log.info("✅ Kiírva: %s", path)
        log.info("   doboz: t1=%s p1=%.5f  →  t2=%s p2=%.5f", t1, p1, t2, p2)
        log.info("   Tedd a TradeForgeViz indikátort egy %s M1 chartra. "
                 "Futtasd újra a szkriptet → a doboz jobb széle nő.", symbol)
    finally:
        mt5_connector.disconnect()


if __name__ == "__main__":
    main()
