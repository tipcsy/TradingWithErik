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
    kind:   str = "strategy"  # "strategy" | "countdown" | "marker"
    timeframe_min: int = 0    # countdown esetén: az időkeret percben
    # marker esetén: a stádiumok (feltételek) sorrendben — (stádium_kulcs, felirat).
    # A megjelenítés stádiumonként EGY kört rajzol; a kör színe/glifája a
    # strategy_cells[strategy_name][stádium_kulcs] cellából jön (glifa=szöveg,
    # szín=szín-név). `strategy_name`: MELYIK stratégiát mutatja ez a jelölő-oszlop
    # (több-stratégia: oszloponként egy). Üres = az egyetlen/aktív stratégia.
    stages: tuple = ()
    strategy_name: str = ""


def StrategyColumn(key: str, header: str, width: int = 8, anchor: str = "center") -> Column:
    """Kényelmi konstruktor stratégia-specifikus oszlophoz."""
    return Column(key=key, header=header, width=width, anchor=anchor, kind="strategy")


def MarkerColumn(key: str, header: str, stages, width: int | None = None,
                 strategy_name: str = "") -> Column:
    """Körös jelölő-oszlop: stádiumonként EGY kör (`● ● ●`). A `stages` egy
    (stádium_kulcs, felirat) sorozat; a stratégia a `compute_display`/`live_cells`
    a stádium-kulcsokra ad kör-glifás cellát (`Cell("●", szín-név)`). A fejléc
    általában a stratégia neve. `strategy_name`: melyik stratégiát mutatja (több-
    stratégia). A szélesség alapból a körök számából adódik."""
    stages = tuple(tuple(s) for s in stages)
    w = width if width is not None else max(len(header), 2 * len(stages) + 1)
    return Column(key=key, header=header, width=w, anchor="center",
                  kind="marker", stages=stages, strategy_name=strategy_name)


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
    # KERETRENDSZER-szintű no-trade órák (0..23, SZERVER/chart idő) — a keret tölti
    # a stratégia-hatókörű `trade_hours` KOMPLEMENTERÉVEL. A stratégia ezekben az
    # órákban RESETELI a jelzési állapotot (a szünet után nulláról fegyverkezik), így
    # nem visz át a szüneten egy elavult szetupot. Üres → nincs no-trade reset
    # (visszafelé kompatibilis: aki nem tölti, a régi viselkedést kapja).
    no_trade_hours: set = field(default_factory=set)

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

    def signal_warmup_bars(self, params: dict, timeframe_label: str) -> int:
        """Hány gyertyát kell VISSZAJÁTSZANI, hogy a JELZÉS-ÁLLAPOT (állapotgép)
        helyesen konvergáljon. Ez MÉLYEBB lehet, mint az indikátor-warmup
        (`warmup_bars`), ha a jelzés a teljes előzménytől függ — pl. egy nyitott/
        zárt „jó zóna", amit egy RÉGI extrém élesített. A live motor első
        bemelegítése és a kijelzés-rekonstrukció EZT használja, hogy a viz-zel
        (`visual_lookback_bars`) egyező ablakállapotot adjon. Alap: = warmup_bars
        (állapotmentes stratégiánál nincs plusz mélység)."""
        return self.warmup_bars(params, timeframe_label)

    @abstractmethod
    def compute_display(self, md: MarketData) -> dict[str, Cell]:
        """A stratégia-oszlopok celláinak kiszámítása MEGJELENÍTÉSHEZ.

        A FORMÁLÓDÓ gyertyát is használhatja, hogy gyakori frissítésnél
        ténylegesen mozogjon az érték. A szélsőértékeket (degenerált ablak,
        átmeneti ugrás) itt kell kiszűrni. Kulcs = Column.key.
        """

    # --- Megjelenítés a MOTOR élő állapotából -----------------------------
    def live_cells(self, state: Any, md: MarketData) -> dict[str, Cell]:
        """A stratégia-oszlopok cellái a MOTOR ÉLŐ jelzésállapotából (state),
        NEM külön rekonstrukcióból. Így a tábla pontosan azt mutatja, amivel a
        motor kereskedik (nincs eltérés a compute_display és a motor között).

        Alapértelmezés: visszaesik a rekonstrukcióra (compute_display) — a
        stratégia felülírhatja, hogy a saját state-jét jelenítse meg.
        """
        return self.compute_display(md)

    # --- MT5 chart-vizualizáció (opcionális) ------------------------------
    def visual_lookback_bars(self, params: dict, timeframe_label: str) -> int:
        """Hány gyertya kell a vizualizációhoz az adott időkeretre.

        A megjelenítendő ELŐZMÉNY (pl. SMA-irány szalag) általában MÉLYEBB
        adatablakot igényel, mint a jelzés-warmup (ott csak pár érvényes sor van
        az indikátor bemelegítése után). A viz-csatorna EZT tölti be a
        `visual_objects`-hoz. Alap: 0 → nincs vizualizáció.
        """
        return 0

    def visual_objects(self, md: MarketData) -> list:
        """Rajzolási objektumok a charthoz (strategy.visual primitívek).

        A `md.bars` a `visual_lookback_bars` szerinti MÉLY ablak (a hívó tölti).
        Üres lista = nincs rajzolnivaló. A megjelenítéstől függetlenül, tisztán
        az adatból számol (mint a compute_display). Alap: üres.
        """
        return []

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

    # --- Minőség-értékelés (a stratégiához tartozik) ----------------------
    def grade(self, test_summary: dict, cfg: dict) -> tuple[str, str, str]:
        """Optimalizált eredmény minősítése: (szöveg, szín-név, indok).

        Alapértelmezés: a `core.quality` szabályrendszere a stratégia SAJÁT
        (merge-elt) `quality` küszöbeivel. A küszöbök a stratégia config-jában
        élnek; egy másik stratégia felülírhatja ezt a metódust saját logikával.
        """
        from core.quality import grade as _grade
        return _grade(test_summary, cfg)

    def grade_rank(self, grade_text: str) -> int:
        """A minősítés rangsora (0 = legjobb) — rendezéshez/szűréshez."""
        from core.quality import grade_rank as _rank
        return _rank(grade_text)

    # --- Azonosítás: MT5 magic ---------------------------------------------
    def magic(self, cfg: dict) -> int:
        """A stratégia MT5 magic száma — ezzel rendelhetők a nyitott pozíciók a
        stratégiához. Alap: a broker.magic (egy-stratégiás visszafelé
        kompatibilitás — a meglévő pozíciók magicje nem változik). TÖBB stratégia
        esetén mindegyik adjon EGYEDI magicet (pl. broker.magic + eltolás), hogy a
        pozíciók broker-szinten szétválaszthatók legyenek."""
        return int(cfg.get("broker", {}).get("magic", 0))

    # --- Optimalizálás: paraméter-érvényesség -----------------------------
    def constraints_ok(self, params: dict) -> bool:
        """Érvényes-e a paraméter-kombináció? Az optimalizáló ezzel prune-ol
        (érvénytelen kombináció → kihagyás). Alapértelmezés: minden érvényes."""
        return True

    # --- Backtest-motor jelzés-hookok -------------------------------------
    # A VÁZ backtest-motorja (trading.backtest) EZEKEN át kéri a stratégiától
    # az indikátorokat, a jelzést ÉS a pozíciótervet (SL/TP + belépés-szűrők);
    # a VÉGREHAJTÁS (breakeven/trailing, slot, lot, spread, napi limit) a motoré.
    # A motor STRATÉGIA-FÜGGETLEN: nem ismer konkrét indikátort (pl. 'atr') vagy
    # méretezést — mindent a stratégia dönt el a `bt_entry` hookban. Konvenció: a
    # magasabb időkeret a timeframes()[0], az alsó a timeframes()[1]. A hookok
    # PRECOMPUTED sorokon, szoros ciklusban hívódnak (ezért nem az on_bar_close-t
    # használják, ami minden híváskor újraszámol).
    def bt_indicators(self, df_hi, df_lo, params):
        """Indikátoros DataFrame-ek: (hi, lo). A stratégia SAJÁT oszlopai — a
        motor ezekbe nem néz bele; csak a `bt_*` hookok olvassák."""
        raise NotImplementedError

    def bt_warmup(self, params: dict, timeframe_label: str) -> int:
        """A backtest-szeleteléshez szükséges PONTOS warmup sorok száma az adott
        időkeretre (a live `warmup_bars`-tól eltérhet — ott lehet ráhagyás)."""
        raise NotImplementedError

    def bt_new_state(self, symbol: str):
        """Üres, pár-szintű jelzésállapot a backtest-motorhoz."""
        raise NotImplementedError

    def bt_on_high_close(self, state, hi_row, params):
        """A magasabb tf egy ZÁRT gyertyája → frissített jelzésállapot."""
        raise NotImplementedError

    def bt_on_low_close(self, state, prev_lo_row, lo_row, params) -> str:
        """Az alsó tf egy ZÁRT gyertyája → 'BUY' | 'SELL' | 'NONE'."""
        raise NotImplementedError

    def sl_tp_pips(self, hi_row, params, pip_size):
        """A pozíció SL/TP mérete PIPBEN a magasabb tf aktuális ZÁRT sorából
        (hi_row): `(sl_pips, tp_pips)` VAGY `None` (nincs érvényes méret).

        TISZTA méretezés — szűrő NÉLKÜL. A live_trader ÉS a backtest is EZT hívja,
        így a méretezés stratégia-független (a motor nem ismer 'atr'-t). A stratégia
        SAJÁT indikátoraiból (pl. ATR) számolja. `pip_size` = a pár tick-mérete."""
        raise NotImplementedError

    def bt_entry(self, hi_row, params, pip_size):
        """BACKTEST pozícióterv: belépés-szűrő + méretezés egyben. `(sl_pips,
        tp_pips)` VAGY `None` (kihagyás — szűrő elbukott / nincs méret).

        Alap: nincs extra szűrő → csak a méretezés (`sl_tp_pips`). A stratégia
        felülírhatja, hogy a SAJÁT belépés-szűrőit (pl. volatilitás) is alkalmazza
        — ezt CSAK a backtest hívja (a live piaci szűrői külön kapuk)."""
        return self.sl_tp_pips(hi_row, params, pip_size)
