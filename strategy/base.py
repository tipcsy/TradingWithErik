"""
Stratégia seam — a váz és a konkrét stratégia közötti szerződés.

Ez a modul SZÁNDÉKOSAN tkinter-mentes és MT5-mentes: csak adatszerkezeteket
és egy absztrakt interfészt definiál. Így a stratégia tisztán tesztelhető és a
megjelenítés/adatforrás szabadon cserélhető.

A felelősségmegosztás:
  • A VÁZ adja a fix oszlopokat (Instrumentum, BID, ASK, Változás%, Spread,
    Pozíció, Napi P&L, Opt státusz, Vezérlés) és a visszaszámláló-oszlopokat.
  • A STRATÉGIA adja a saját, középső oszlopait + azok kiszámítását, a
    jelzéslogikát és az optimalizálandó paramétertartományt.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, Any

import pandas as pd


# ---------------------------------------------------------------------------
# Megjelenítési primitívek
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Cell:
    """Egy táblázatcella tartalma: szöveg + szemantikus szín-név.

    A szín-név a dashboard.theme.SEMANTIC kulcsa (pl. "green", "red",
    "muted", "yellow", "white"). A stratégia soha nem ad vissza hex kódot.
    """
    text:  str = "—"
    color: str = "muted"


@dataclass(frozen=True)
class Column:
    """A táblázat egy oszlopának deklarációja (megjelenítési metaadat)."""
    key:    str             # egyedi kulcs; a stratégia ezen tölti a cellát
    header: str             # fejléc szöveg
    width:  int = 8         # karakterszélesség (Courier monospace)
    anchor: str = "center"  # "w" | "center" | "e"
    kind:   str = "strategy"  # "strategy" | "countdown"
    timeframe_min: int = 0    # countdown esetén: az időkeret percben


def StrategyColumn(key: str, header: str, width: int = 8, anchor: str = "center") -> Column:
    """Kényelmi konstruktor stratégia-specifikus oszlophoz."""
    return Column(key=key, header=header, width=width, anchor=anchor, kind="strategy")


def CountdownColumn(timeframe_min: int, header: str, width: int = 7) -> Column:
    """Visszaszámláló oszlop (a következő gyertyazárásig hátralévő idő).

    Az értéket a VÁZ számolja az időkeret alapján; a stratégia csak a
    sorrendbe illeszti.
    """
    return Column(key=f"countdown_{timeframe_min}", header=header,
                  width=width, anchor="center",
                  kind="countdown", timeframe_min=timeframe_min)


@dataclass(frozen=True)
class Timeframe:
    """Egy időkeret, amit a stratégia használ."""
    label:   str   # pl. "M15"
    minutes: int   # pl. 15


# ---------------------------------------------------------------------------
# Piaci adat konténer — a váz tölti, a stratégia fogyasztja
# ---------------------------------------------------------------------------

@dataclass
class MarketData:
    """Egy pár pillanatnyi piaci adata indikátorszámításhoz.

    bars[label] egy OHLC(V) DataFrame, UTC indexszel. A legutolsó sor a még
    FORMÁLÓDÓ (nyitott) gyertya; az utolsó előtti sor az utolsó ZÁRT gyertya.
    """
    symbol: str
    params: dict
    bars:   dict[str, pd.DataFrame] = field(default_factory=dict)

    def closed(self, label: str) -> Optional[pd.Series]:
        df = self.bars.get(label)
        if df is None or len(df) < 2:
            return None
        return df.iloc[-2]

    def forming(self, label: str) -> Optional[pd.Series]:
        df = self.bars.get(label)
        if df is None or len(df) < 1:
            return None
        return df.iloc[-1]


# ---------------------------------------------------------------------------
# Stratégia interfész
# ---------------------------------------------------------------------------

class Strategy(ABC):
    """A vázhoz csatlakozó stratégia szerződése."""

    name: str = "strategy"

    # --- Megjelenítés -----------------------------------------------------
    @abstractmethod
    def timeframes(self) -> list[Timeframe]:
        """Mely időkereteket használja (adatletöltés + visszaszámlálók)."""

    @abstractmethod
    def columns(self) -> list[Column]:
        """A stratégia-specifikus oszlopok (a fix oszlopok közé kerülnek).
        Visszaszámláló-oszlopok (CountdownColumn) is elhelyezhetők itt."""

    @abstractmethod
    def warmup_bars(self, params: dict, timeframe_label: str) -> int:
        """Hány gyertya kell az indikátorok bemelegítéséhez az adott időkeretre."""

    @abstractmethod
    def compute_display(self, md: MarketData) -> dict[str, Cell]:
        """A stratégia-oszlopok celláinak kiszámítása MEGJELENÍTÉSHEZ.

        A FORMÁLÓDÓ gyertyát is használhatja, hogy gyakori frissítésnél
        ténylegesen mozogjon az érték. A szélsőértékeket (degenerált ablak,
        átmeneti ugrás) itt kell kiszűrni. Kulcs = Column.key.
        """

    # --- Élő jelzéslogika (a futtatómotor használja, ZÁRT gyertyán) -------
    @abstractmethod
    def new_signal_state(self, symbol: str) -> Any:
        """Üres, pár-szintű jelzésállapot (a motor tartja életben)."""

    @abstractmethod
    def on_bar_close(self, state: Any, md: MarketData) -> tuple[Any, str]:
        """ZÁRT gyertyák alapján frissíti az állapotot és belépési jelet ad.

        Visszaad: (frissített_state, jel) ahol jel ∈ {"BUY","SELL","NONE"}.
        A megjelenítéstől FÜGGETLEN, determinisztikus logika.
        """

    # --- Optimalizálás ----------------------------------------------------
    @abstractmethod
    def base_params(self, cfg: dict) -> dict:
        """A stratégia alap-paraméterei a config-ból (indikátorok+sltp+pozíció)."""

    @abstractmethod
    def param_space(self, cfg: dict, base_params: dict, method: str,
                    max_trials: int) -> list[dict]:
        """Optimalizálandó paraméter-kombinációk listája."""
