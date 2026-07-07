"""
Főindító script.

Parancsok:
  python main.py download     — historikus adatok letöltése MT5-ből
  python main.py optimize     — AI paraméter optimalizálás (összes aktív pár)
  python main.py optimize EURUSD GBPJPY  — csak megadott párok optimalizálása
  python main.py live         — élő kereskedés + dashboard
  python main.py dashboard    — csak dashboard (demo mód, MT5 nélkül)
  python main.py backtest     — backtest futtatás az alapértelmezett paraméterekkel
"""

import json
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CFG_PATH = ROOT / "config.json"


def load_cfg() -> dict:
    # A váz config.json + az aktív stratégia saját beállításainak beolvasztása.
    from strategy.settings import load_config
    return load_config(CFG_PATH)


def cmd_download():
    from tools.download_history import main
    main()


def cmd_optimize(symbols=None):
    from ml.optimizer import run_optimizer
    cfg = load_cfg()
    run_optimizer(cfg, symbols or None)


def cmd_backtest():
    from trading.backtest import run_backtest
    cfg = load_cfg()
    run_backtest(cfg)


def cmd_live():
    from core import mt5_connector
    from core.risk_manager import SlotManager
    from trading.live_trader import run, dashboard, instrument_state, optimizer_status
    from dashboard.gui import DashboardWindow

    cfg = load_cfg()

    if not mt5_connector.connect(cfg):
        sys.exit(1)

    slot_mgr = SlotManager(cfg["trading"]["max_open_slots"])

    # Live trader szálban fut
    trader_thread = threading.Thread(
        target=run,
        args=(cfg, slot_mgr),
        daemon=True,
        name="LiveTrader",
    )
    trader_thread.start()

    # Rövid várakozás hogy a live_trader inicializálja a dashboard/instrument_state dict-eket
    import time
    time.sleep(1)

    # Dashboard főszálon (tkinter csak főszálból futhat)
    def on_slots_change(new_max):
        slot_mgr.max_slots = new_max

    win = DashboardWindow(
        cfg, dashboard, instrument_state, optimizer_status,
        on_play_pair=None,   # instrument_state váltás elegendő, a run() loop felkapja
        on_stop_pair=None,
        on_slots_change=on_slots_change,
    )

    def update_header():
        balance = mt5_connector.account_balance()
        win.set_balance(balance)
        free = slot_mgr.free()
        win.set_slots(free, slot_mgr.max_slots)
        win.root.after(5000, update_header)

    win.root.after(1000, update_header)

    try:
        win.run()
    finally:
        mt5_connector.disconnect()


def cmd_dashboard():
    """Demo mód — MT5 nélkül, szimulált adatokkal."""
    from dashboard.gui import DashboardWindow, _demo_dashboard
    cfg = load_cfg()
    db, inst_state, opt_status, n_pos = _demo_dashboard(cfg)
    win = DashboardWindow(
        cfg, db, inst_state, opt_status,
        on_play_pair=None,
        on_stop_pair=None,
    )
    max_s = cfg["trading"]["max_open_slots"]
    win.set_balance(1024.50)
    win.set_slots(free=max(0, max_s - n_pos), max_s=max_s)
    win.run()


COMMANDS = {
    "download":  (cmd_download,   []),
    "optimize":  (cmd_optimize,   "symbols"),
    "backtest":  (cmd_backtest,   []),
    "live":      (cmd_live,       []),
    "dashboard": (cmd_dashboard,  []),
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(0)

    cmd = sys.argv[1]
    fn, arg_spec = COMMANDS[cmd]

    if arg_spec == "symbols":
        symbols = sys.argv[2:] if len(sys.argv) > 2 else None
        fn(symbols)
    else:
        fn()
