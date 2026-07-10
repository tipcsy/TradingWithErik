"""
MT5 vizualizáció — MINIMÁLIS PRÓBA (végpontig működő csővezeték).

Kiír egy rövid per-gyertya SÁV-CSÍKOT a `TradeForgeBands.mq5` al-ablaknak: a
megadott szimbólum utolsó ~20 M1 gyertyájára STATE sorokat (zöld trend + kék
M15-ablak jelölés), amit az indikátor színbufferbe tölt.

Cél: bizonyítani a teljes csatornát (Python → Common\\Files fájl → MQL5
indikátor → al-ablak sávjai). Tedd a TradeForgeViz-t egy chartra (az auto-felrakja
a TradeForgeBands al-ablakot is), és futtasd a szkriptet → megjelenik a csík.

Futtatás:  python tools/viz_test.py [SYMBOL]   (alapértelmezett: EURUSD)
"""

import logging
import sys
from pathlib import Path

import MetaTrader5 as mt5

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import mt5_connector, mt5_visual
from strategy.visual import BarState
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
        # Az utolsó 20 gyertyára egy próba-csík: zöld trend végig, a középső ötnél
        # kék M15-ablak-jelölés is → mindhárom sáv-mechanika látszik.
        cells = []
        for k in range(20, 0, -1):
            t = int(rates["time"][-k])
            window = 1 if 8 <= k <= 12 else 0
            cells.append(BarState(t=t, notrade=0, dir=1, window=window))
        path = mt5_visual.write(symbol, cells)

        if path is None:
            log.error("Nem sikerült a Common\\Files fájl írása.")
            sys.exit(1)
        log.info("✅ Kiírva: %s (%d STATE cella)", path, len(cells))
        log.info("   Tedd a TradeForgeViz indikátort egy %s M1 chartra (auto-"
                 "felrakja a TradeForgeBands al-ablakot) → megjelenik a csík.", symbol)
    finally:
        mt5_connector.disconnect()


if __name__ == "__main__":
    main()
