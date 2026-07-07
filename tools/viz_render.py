"""
MT5 vizualizáció — a STRATÉGIA rajzolása egy szimbólumra, ÉLES motor nélkül.

Betölti a configot + stratégiát + (optimalizált) paramétereket, mély adatablakot
tölt, és egyszer kiírja a stratégia `visual_objects` objektumait a viz-fájlba —
így a chartra tett TradeForgeViz indikátor megjeleníti (pl. az SMA-irány szalagot).

Futtatás:  python tools/viz_render.py [SYMBOL]   (alapértelmezett: EURUSD)
"""

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import mt5_connector
from strategy import get_strategy
from strategy.settings import load_config
from trading.live_trader import write_pair_visuals, load_pair_params

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
        strategy = get_strategy(cfg)
        params   = load_pair_params(symbol) or strategy.base_params(cfg)
        pair_cfg = cfg.get("pairs", {}).get(symbol, {})
        pip_size = pair_cfg.get("pip_size", 0.0001)

        write_pair_visuals(symbol, params, strategy, pip_size)
        log.info("✅ %s — viz kiírva. Tedd a TradeForgeViz indikátort egy %s M15 "
                 "chartra (vagy futtasd újra, ha már fent van).", symbol, symbol)
    finally:
        mt5_connector.disconnect()


if __name__ == "__main__":
    main()
