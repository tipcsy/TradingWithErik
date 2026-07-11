"""
MT5 chart-vizualizáció ÍRÓCSATORNA.

A stratégia rajzolási primitíveket ad (`strategy.visual`: Rect/VLine/Trend/Text/
Label); ez a modul ezeket az MT5 Common\\Files mappájába sorosítja, ahonnan a
`TradeForgeViz.mq5` indikátor felolvassa és kirajzolja. A Python NEM tud MT5
chartra rajzolni (a MetaTrader5 API csak adat + kereskedés — nincs benne
objektum-/chart-vezérlés), ezért fájl + indikátor a csatorna.

A fájl a kívánt ÁLLAPOT teljes PILLANATKÉPE: minden objektum a jelenlegi
méretével újra kiíródik, az indikátor upsert-el és sosem töröl (lásd
strategy.visual). Az idő NYERS bar-idő integer (a copy_rates adja), így nincs
időzóna-csúszás. Írás atomikus (temp → replace), hogy az indikátor sose olvasson
fél fájlt.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional

import MetaTrader5 as mt5

from core.mt5_connector import MT5_LOCK
from strategy.visual import PREFIX


def files_dir() -> Optional[Path]:
    """Az MT5 Common\\Files mappa (FILE_COMMON). None, ha nincs kapcsolat."""
    with MT5_LOCK:
        info = mt5.terminal_info()
    if info is None:
        return None
    return Path(info.commondata_path) / "Files"


def write_lines(symbol: str, lines: list) -> Optional[Path]:
    """A `symbol` teljes pillanatképét kiírja `TFV_<symbol>.csv`-be ELŐRE
    sorosított sorokból (több-stratégiás viz: a hívó stratégiánként `tag_line`-nal
    megjelölt sorokat ad). Atomikus (temp → replace)."""
    d = files_dir()
    if d is None:
        return None
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{PREFIX}{symbol}.csv"
    tmp  = path.with_suffix(".csv.tmp")

    payload = "\n".join(lines)
    if payload:
        payload += "\n"
    tmp.write_text(payload, encoding="ascii", errors="replace")

    # os.replace atomikus egy köteten belül. Ritka verseny: az MQL5 épp olvassa
    # → Windows PermissionError; pár próbálkozás elég (a beolvasás mikroszekundum).
    for _ in range(5):
        try:
            os.replace(tmp, path)
            return path
        except PermissionError:
            time.sleep(0.05)
    return None


def write(symbol: str, objects: list) -> Optional[Path]:
    """A `symbol` objektum-pillanatképe (nem-tagelt, egy-stratégiás). Visszafelé
    kompatibilis burok a `write_lines` fölött."""
    return write_lines(symbol, [o.line() for o in objects])


def clear(symbol: str) -> Optional[Path]:
    """A `symbol` chart-objektumainak TÖRLÉSE: egy `CLEAR` direktívát ír a
    fájlba, amit az indikátor a saját (TFV_ prefixű) objektumai törlésével
    értelmez. A vizualizáció KI-kapcsolásához (V gomb) használjuk."""
    d = files_dir()
    if d is None:
        return None
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{PREFIX}{symbol}.csv"
    tmp  = path.with_suffix(".csv.tmp")
    tmp.write_text("CLEAR\n", encoding="ascii")
    for _ in range(5):
        try:
            os.replace(tmp, path)
            return path
        except PermissionError:
            time.sleep(0.05)
    return None
