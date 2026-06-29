"""
Historikus M15 és M1 adatok letöltése MT5-ből minden engedélyezett párhoz.

Stratégia (a Trading-with-ai projektből átvéve):
  - Alap TF (M15, M1): tick -> resample, havi bontásban.
    Ez adja a maximális visszatekintési időszakot (2-3 év) és az avg_spread mezőt.
  - Ha a fájl már létezik: csak a hiányzó (gap) adatokat tölti le és fűzi hozzá.

Futtatás: python tools/download_history.py
"""

import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import MetaTrader5 as mt5
import pandas as pd

# Importáljuk a globális MT5 lockot — ha elérhető (GUI-ból hívva); egyébként dummy
try:
    from core.mt5_connector import MT5_LOCK as _MT5_LOCK
except Exception:
    import threading as _threading
    _MT5_LOCK = _threading.Lock()  # standalone futásnál saját lock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import mt5_connector

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

TF_MAP = {
    "M1":  mt5.TIMEFRAME_M1,
    "M15": mt5.TIMEFRAME_M15,
}

RESAMPLE = {
    "M1":  "1min",
    "M15": "15min",
}


# ---------------------------------------------------------------------------
# Segédfüggvények (tick -> bar konverzió)
# ---------------------------------------------------------------------------

def _next_month(dt: datetime) -> datetime:
    if dt.month == 12:
        return dt.replace(year=dt.year + 1, month=1, day=1,
                          hour=0, minute=0, second=0, microsecond=0)
    return dt.replace(month=dt.month + 1, day=1,
                      hour=0, minute=0, second=0, microsecond=0)


def _ticks_to_bars(ticks_raw, freq: str) -> pd.DataFrame:
    """MT5 tick tömb -> OHLCV DataFrame (UTC index, avg_spread mezővel)."""
    df = pd.DataFrame(ticks_raw)
    if df.empty:
        return pd.DataFrame()

    df["dt"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.set_index("dt").sort_index()

    has_bid  = "bid"  in df.columns
    has_ask  = "ask"  in df.columns
    has_last = "last" in df.columns

    if has_bid and has_ask:
        df["mid"] = (df["bid"] + df["ask"]) / 2.0
        if has_last:
            mask = df["last"] > 0
            df.loc[mask, "mid"] = df.loc[mask, "last"]
        valid = (df["bid"] > 0) & (df["ask"] > 0) & (df["ask"] > df["bid"])
        df["spread"] = float("nan")
        df.loc[valid, "spread"] = df.loc[valid, "ask"] - df.loc[valid, "bid"]
    elif has_last:
        df["mid"] = df["last"]
    else:
        return pd.DataFrame()

    df = df[df["mid"] > 0].dropna(subset=["mid"])
    if df.empty:
        return pd.DataFrame()

    ohlcv = df["mid"].resample(freq).ohlc()
    ohlcv["volume"] = df["mid"].resample(freq).count()
    ohlcv = ohlcv.dropna(subset=["open"])
    ohlcv = ohlcv[ohlcv["volume"] > 0]

    if "spread" in df.columns:
        ohlcv["avg_spread"] = df["spread"].resample(freq).mean()

    return ohlcv


def _download_via_ticks(symbol: str, tf_str: str, start: datetime, end: datetime) -> pd.DataFrame:
    """
    Bars letöltése tick -> resample módszerrel, havi bontásban.
    Visszatér: összesített OHLCV DataFrame, vagy üres DataFrame ha nincs adat.
    """
    freq = RESAMPLE[tf_str]
    cur = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    frames = []

    while cur < end:
        chunk_end = min(_next_month(cur), end)
        print(f"      {cur.strftime('%Y-%m')} ...", end="", flush=True)
        t0 = time.time()

        with _MT5_LOCK:
            ticks_raw = mt5.copy_ticks_range(symbol, cur, chunk_end, mt5.COPY_TICKS_ALL)

        if ticks_raw is None or len(ticks_raw) == 0:
            err = mt5.last_error()
            note = str(err) if err[0] != 0 else "üres időszak"
            print(f" nincs adat ({note})")
            cur = _next_month(cur)
            continue

        ohlcv = _ticks_to_bars(ticks_raw, freq)
        if ohlcv.empty:
            print(f" {len(ticks_raw):,} tick -> 0 bar")
            cur = _next_month(cur)
            continue

        frames.append(ohlcv)
        print(f" {len(ticks_raw):>8,} tick -> {len(ohlcv):>5,} bar  ({time.time()-t0:.1f}s)")
        cur = _next_month(cur)
        time.sleep(0.05)

    if not frames:
        return pd.DataFrame()

    combined = pd.concat(frames)
    combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    return combined


# ---------------------------------------------------------------------------
# Gap feltöltés meglévő parquet-hez
# ---------------------------------------------------------------------------

def _fill_gap(out_file: Path, symbol: str, tf_str: str, end: datetime) -> bool:
    """
    Meglévő parquet fájlba hozzáírja az utolsó bar óta hiányzó adatokat.
    Visszatér: True ha sikeres, False ha nem volt mit tölteni.
    """
    existing = pd.read_parquet(out_file)
    last_dt = existing.index[-1]

    # Timezone-aware -> naive UTC összehasonlításhoz
    if last_dt.tzinfo is not None:
        last_dt_naive = last_dt.replace(tzinfo=None)
    else:
        last_dt_naive = last_dt

    gap_h = (end.replace(tzinfo=None) - last_dt_naive).total_seconds() / 3600
    if gap_h < 1:
        log.info("%s %s — naprakész, kihagyva.", symbol, tf_str)
        return True

    log.info("%s %s — gap %.0fh -> frissítés (natív bar)...", symbol, tf_str, gap_h)
    from_dt = last_dt.replace(tzinfo=timezone.utc) if last_dt.tzinfo is None else last_dt
    from_dt = from_dt + pd.Timedelta(minutes=1)

    tf_mt5 = TF_MAP[tf_str]
    with _MT5_LOCK:
        new_raw = mt5.copy_rates_range(symbol, tf_mt5,
                                       from_dt.replace(tzinfo=None),
                                       end.replace(tzinfo=None))
    if new_raw is None or len(new_raw) == 0:
        log.warning("%s %s — gap feltöltés: nincs adat (%s)", symbol, tf_str, mt5.last_error())
        return False

    df_new = pd.DataFrame(new_raw)
    df_new["time"] = pd.to_datetime(df_new["time"], unit="s", utc=True)
    df_new = df_new.set_index("time")
    df_new = df_new[["open", "high", "low", "close", "tick_volume"]].rename(
        columns={"tick_volume": "volume"}
    )

    # Timezone egységesítés: mindkettő UTC-aware legyen
    if existing.index.tzinfo is None:
        existing.index = existing.index.tz_localize("UTC")
    if df_new.index.tzinfo is None:
        df_new.index = df_new.index.tz_localize("UTC")

    combined = pd.concat([existing, df_new])
    combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    combined.to_parquet(out_file)
    log.info("%s %s — +%d bar hozzáadva -> összesen %d", symbol, tf_str, len(df_new), len(combined))
    return True


# ---------------------------------------------------------------------------
# Egy pár letöltése
# ---------------------------------------------------------------------------

def download_pair(symbol: str, tf_str: str, start: datetime, overwrite: bool, end: datetime) -> bool:
    out_dir = ROOT / "data" / tf_str.lower()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{symbol}.parquet"

    if not mt5.symbol_select(symbol, True):
        log.warning("%s — nem érhető el ezen a brókerszerveren, kihagyva.", symbol)
        return False

    # Ha létezik és nem overwrite: gap feltöltés
    if out_file.exists() and not overwrite:
        return _fill_gap(out_file, symbol, tf_str, end)

    # Teljes letöltés tick -> resample módszerrel
    log.info("%s %s — tick letöltés (%s -> %s)...", symbol, tf_str,
             start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
    ohlcv = _download_via_ticks(symbol, tf_str, start, end)

    if ohlcv.empty:
        log.warning("%s %s — nincs letölthető adat.", symbol, tf_str)
        return False

    ohlcv.to_parquet(out_file)
    log.info("%s %s — %d gyertya mentve -> %s  [%s -> %s]",
             symbol, tf_str, len(ohlcv), out_file.name,
             str(ohlcv.index[0])[:10], str(ohlcv.index[-1])[:10])
    return True


# ---------------------------------------------------------------------------
# Belépési pont
# ---------------------------------------------------------------------------

def main():
    cfg_path = ROOT / "config.json"
    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)

    if not mt5_connector.connect(cfg):
        sys.exit(1)

    start_dt = datetime.strptime(
        cfg["data"]["history_start_date"], "%Y-%m-%d"
    ).replace(tzinfo=timezone.utc)
    end_dt   = datetime.now(timezone.utc)
    overwrite = cfg["data"].get("overwrite_existing", False)

    pairs = {s: p for s, p in cfg["pairs"].items()
             if isinstance(p, dict) and p.get("enabled", False)}
    log.info("%d aktív pár letöltése indul...", len(pairs))

    ok = err = 0
    for symbol in pairs:
        for tf in ("M15", "M1"):
            if download_pair(symbol, tf, start_dt, overwrite, end_dt):
                ok += 1
            else:
                err += 1

    mt5_connector.disconnect()
    log.info("Kész. Sikeres: %d | Hibás: %d", ok, err)


if __name__ == "__main__":
    main()
