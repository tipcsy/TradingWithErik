"""
MT5 vizualizáció — egy szimbólum chart-objektumainak TÖRLÉSE.

`CLEAR` direktívát ír a viz-fájlba, amit a TradeForgeViz indikátor a saját
(TFV_ prefixű) objektumai törlésével értelmez. Hasznos a viz-logika módosítása
után, amikor a régi (upsert miatt meg nem törölt) objektumokat le kell szedni.

Futtatás:  python tools/viz_clear.py [SYMBOL]   (alapértelmezett: EURUSD)
FELTÉTEL: a TradeForgeViz.mq5 újra van fordítva a CLEAR támogatással.
"""

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import mt5_connector, mt5_visual
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
        path = mt5_visual.clear(symbol)
        if path is None:
            log.error("Nem sikerült a CLEAR írása (Common\\Files nem elérhető).")
            sys.exit(1)
        log.info("✅ %s — CLEAR kiírva. Az indikátor ~1 mp-en belül letörli a "
                 "TFV_ objektumokat. Utána futtasd újra a viz_render-t a tiszta rajzhoz.",
                 symbol)
    finally:
        mt5_connector.disconnect()


if __name__ == "__main__":
    main()
