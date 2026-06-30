import MetaTrader5 as mt5
import logging
import threading
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# MT5 Python API nem thread-safe — minden hívást ezen a lockon keresztül kell intézni
MT5_LOCK = threading.Lock()


def connect(cfg: dict) -> bool:
    """MT5 inicializálás és bejelentkezés a config alapján."""
    broker = cfg["broker"]
    mt5_cfg = cfg.get("mt5", {})

    kwargs = {}
    if "path" in mt5_cfg:
        kwargs["path"] = mt5_cfg["path"]
    if mt5_cfg.get("portable"):
        kwargs["portable"] = True

    with MT5_LOCK:
        if not mt5.initialize(**kwargs):
            log.error("MT5 initialize hiba: %s", mt5.last_error())
            return False

        if not mt5.login(
            broker["login"],
            password=broker["password"],
            server=broker["server"],
        ):
            log.error("MT5 login hiba: %s", mt5.last_error())
            mt5.shutdown()
            return False

        info = mt5.account_info()

    log.info(
        "MT5 kapcsolódva | %s | Egyenleg: %.2f %s | Demo: %s",
        broker["server"],
        info.balance,
        info.currency,
        broker.get("is_demo", True),
    )
    return True


def disconnect():
    with MT5_LOCK:
        mt5.shutdown()
    log.info("MT5 kapcsolat lezárva.")


def account_balance() -> float:
    with MT5_LOCK:
        info = mt5.account_info()
    return info.balance if info else 0.0


def account_currency() -> str:
    with MT5_LOCK:
        info = mt5.account_info()
    return info.currency if info else "USD"


def daily_pnl() -> Optional[float]:
    """
    Mai nap lezárt ügyletek összesített P&L-je MT5-ből.
    None ha nem kapcsolódtunk, 0.0 ha nem volt kereskedés.
    """
    try:
        from datetime import date, datetime, timezone, timedelta
        today    = datetime.combine(date.today(), datetime.min.time()).replace(tzinfo=timezone.utc)
        tomorrow = today + timedelta(days=1)
        with MT5_LOCK:
            deals = mt5.history_deals_get(today, tomorrow)
        if deals is None:
            return None
        return sum(d.profit + d.commission + d.swap
                   for d in deals if d.entry == 1)
    except Exception:
        return None


def open_positions_by_symbol() -> dict:
    """
    Visszaadja az MT5-ben nyitott pozíciókat szimbólum szerint AGGREGÁLVA.
    Egy szimbólumon több pozíció is lehet → összegzett P&L + darabszám.
    {symbol: {"pnl": float, "count": int, "direction": "BUY"|"SELL"|"MIX",
              "risk_free": False}}
    """
    try:
        with MT5_LOCK:
            positions = mt5.positions_get()
        if not positions:
            return {}
        result = {}
        for pos in positions:
            agg = result.setdefault(pos.symbol, {
                "pnl": 0.0, "count": 0, "direction": None, "risk_free": False})
            agg["pnl"]   += pos.profit
            agg["count"] += 1
            d = "BUY" if pos.type == 0 else "SELL"
            agg["direction"] = d if agg["direction"] in (None, d) else "MIX"
        for agg in result.values():
            agg["pnl"] = round(agg["pnl"], 2)
        return result
    except Exception:
        return {}


def is_connected() -> bool:
    try:
        with MT5_LOCK:
            info = mt5.account_info()
        return info is not None
    except Exception:
        return False


def connection_info(cfg: dict) -> dict:
    """
    Visszaadja a kapcsolat állapotát és a számla adatait.
    Demo módban (MT5 nem elérhető) a config-ból tölt.
    """
    try:
        with MT5_LOCK:
            info = mt5.account_info()
        if info is not None:
            return {
                "connected": True,
                "login":     info.login,
                "server":    info.server,
                "name":      info.name,
                "balance":   info.balance,
                "currency":  info.currency,
                "is_demo":   info.trade_mode == 0,
            }
    except Exception:
        pass

    broker = cfg.get("broker", {})
    return {
        "connected": False,
        "login":     broker.get("login", "—"),
        "server":    broker.get("server", "—"),
        "name":      "—",
        "balance":   0.0,
        "currency":  "—",
        "is_demo":   broker.get("is_demo", True),
    }
