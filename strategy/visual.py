"""
Rajzolási primitívek a chart-vizualizációhoz (MT5-MENTES seam-modul).

A stratégia ezeket adja vissza a `visual_objects()`-ból; a `core.mt5_visual`
sorosítja őket az MT5 Common\\Files fájlba, ahonnan a `TradeForgeViz.mq5`
indikátor kirajzolja. Ez a modul SZÁNDÉKOSAN nem importál MetaTrader5-öt (mint a
base.py), hogy a stratégia és a tesztek MT5 nélkül is futtathatók legyenek.

Elv (mint a Cell): a stratégia SZEMANTIKUS színnevet ad; a sorosítás fordítja
"r,g,b" hármassá (az MQL5 `StringToColor` ezt érti). Minden objektumnak STABIL
neve van → az indikátor upsert-el (létrehoz vagy módosít), sosem töröl, így egy
objektum (pl. SMA-doboz) csak NŐ, amíg tart a feltétel.
"""

from __future__ import annotations

from dataclasses import dataclass

# Minden objektum neve ezzel a prefixszel kezdődik — az indikátor ez alapján
# ismeri fel a SAJÁT objektumait (kézzel rajzolt objektumhoz nem nyúl).
PREFIX = "TFV_"

# Szemantikus szín-név → (R, G, B). A stratégia sosem ad hex/rgb kódot.
COLORS: dict[str, tuple[int, int, int]] = {
    "green":  (0, 170, 0),
    "lime":   (0, 255, 0),
    "red":    (220, 0, 0),
    "blue":   (0, 120, 255),
    "yellow": (240, 210, 0),
    "orange": (255, 140, 0),
    "white":  (255, 255, 255),
    "black":  (0, 0, 0),
    "gray":   (128, 128, 128),
    "muted":  (110, 110, 110),
    "magenta": (230, 40, 230),   # átlagár (null pont) — erős, jól elkülönülő
    "cyan":    (0, 220, 220),     # ráépítés-küszöb (ref_close)
}


def _rgb(color: str) -> str:
    r, g, b = COLORS.get(color, COLORS["white"])
    return f"{r},{g},{b}"


def _clean(text: str) -> str:
    """A szöveges mezőkből eltávolítjuk az elválasztót és a sortörést."""
    return text.replace(";", ",").replace("\n", " ").replace("\r", " ")


def _name(name: str) -> str:
    return name if name.startswith(PREFIX) else PREFIX + name


# A stratégia-tag elválasztója az objektum-névben: TFV_<strategy>@<eredeti_név>.
# Így az MQL5 indikátor egy `InpStrategy` input alapján szűrhet (a névre), és
# TÖBB stratégia objektumai UGYANABBAN a fájlban sem ütköznek (upsert stratégiánként).
STRAT_SEP = "@"


def tag_line(line: str, strategy: str) -> str:
    """Egy sorosított objektum-sort megjelöl a stratégia nevével (több-stratégiás
    viz: minden stratégia UGYANABBA a szimbólum-fájlba ír, az indikátor szűr).

    - Nevesített objektum (RECT/VLINE/TREND/ARROW/TEXT/LABEL): a NÉV mezőt
      namespace-eljük: `TFV_<eredeti>` → `TFV_<strategy>@<eredeti>`.
    - Névtelen sor (STATE/IND/ALERT): a stratégiát a TÍPUS UTÁN szúrjuk be
      (`STATE;<strat>;…`), mert az IND változó-hosszú szint-listája miatt a sor
      VÉGE nem egyértelmű.
    - CLEAR: érintetlen (direktíva).
    `strategy` üres → a sor változatlan (egy-stratégiás, régi viselkedés)."""
    if not strategy:
        return line
    typ, sep, rest = line.partition(";")
    if typ == "CLEAR":
        return line
    if typ in ("STATE", "IND", "ALERT"):
        return f"{typ};{strategy};{rest}" if sep else f"{typ};{strategy}"
    fields = line.split(";")
    if len(fields) >= 2 and fields[1].startswith(PREFIX):
        fields[1] = PREFIX + strategy + STRAT_SEP + fields[1][len(PREFIX):]
    return ";".join(fields)


# ---------------------------------------------------------------------------
# Primitívek — mindegyik egy `;`-elválasztott sorrá sorosítható (.line()).
# ---------------------------------------------------------------------------

@dataclass
class Rect:
    """Telített téglalap (pl. SMA-irány doboz). Két sarok: (t1,p1)–(t2,p2)."""
    name: str
    t1: int
    p1: float
    t2: int
    p2: float
    color: str = "green"
    fill: bool = True

    def line(self) -> str:
        return ";".join([
            "RECT", _name(self.name),
            str(int(self.t1)), repr(float(self.p1)),
            str(int(self.t2)), repr(float(self.p2)),
            _rgb(self.color), "1" if self.fill else "0",
        ])


@dataclass
class VLine:
    """Függőleges vonal egy időpontnál (pl. M15 jelzés / M1 belépő jelölés)."""
    name: str
    t1: int
    color: str = "yellow"
    width: int = 1

    def line(self) -> str:
        return ";".join([
            "VLINE", _name(self.name),
            str(int(self.t1)), _rgb(self.color), str(int(self.width)),
        ])


@dataclass
class Trend:
    """Trendvonal (sugár nélkül): (t1,p1)–(t2,p2). Pl. 6-gyertyás TP/SL szint.
    style: MT5 vonalstílus (0=folytonos, 1=szaggatott, 2=pont, …) — a szaggatott
    CSAK width=1-nél látszik. A valós kötés SL/TP-jét szaggatottal különböztetjük
    meg a replay tömör szegmensétől."""
    name: str
    t1: int
    p1: float
    t2: int
    p2: float
    color: str = "green"
    width: int = 1
    style: int = 0

    def line(self) -> str:
        return ";".join([
            "TREND", _name(self.name),
            str(int(self.t1)), repr(float(self.p1)),
            str(int(self.t2)), repr(float(self.p2)),
            _rgb(self.color), str(int(self.width)), str(int(self.style)),
        ])


@dataclass
class Arrow:
    """Nyíl egy (idő, ár) ponton — pl. VALÓS kötés belépő-jelölése (MT5 deal). A
    jel-vonalaktól (VLine, replay) SZÁNDÉKOSAN eltérő alakzat: a betöltési áron ül a
    gyertyán, így egyből látszik, melyik jelből lett tényleges trade. `code` =
    Wingdings nyíl-kód (233 fel = BUY, 234 le = SELL)."""
    name: str
    t1: int
    p1: float
    code: int = 233
    color: str = "white"
    width: int = 1

    def line(self) -> str:
        return ";".join([
            "ARROW", _name(self.name),
            str(int(self.t1)), repr(float(self.p1)),
            str(int(self.code)), _rgb(self.color), str(int(self.width)),
        ])


@dataclass
class Text:
    """Chart-hoz (idő/ár) horgonyzott szöveg."""
    name: str
    t1: int
    p1: float
    text: str
    color: str = "white"
    fontsize: int = 9

    def line(self) -> str:
        return ";".join([
            "TEXT", _name(self.name),
            str(int(self.t1)), repr(float(self.p1)),
            _rgb(self.color), str(int(self.fontsize)), _clean(self.text),
        ])


@dataclass
class BarState:
    """Per-M15-gyertya SÁV-ÁLLAPOT a dedikált al-ablakhoz (TradeForgeBands).

    Nem klasszikus rajz-objektum (nincs neve/upsertje): a Python gyertyánként egy
    STATE sort ad, az indikátor SZÍNBUFFERBE (DRAW_COLOR_HISTOGRAM2) tölti — három
    fix magasságú sávban: szürke no-trade / zöld-piros trend / kék M15-ablak.

    Mezők:
      t:       nyers bar-idő (epoch, mint a copy_rates)
      notrade: 1 ha az adott gyertya no-trade órában van (különben 0)
      dir:     -1 SELL (piros), 0 nincs, 1 BUY (zöld) — az SMA-irány
      window:  1 ha aktív az M15 jelzési ablak, különben 0

    A no-trade maszkolást a KÜLDŐ (live_trader) végzi: no-trade gyertyánál
    notrade=1 ÉS dir=0, window=0 (így a Viz csak a szürkét mutatja). A stratégia
    az órákról nem tud → mindig notrade=0-t ad, a keret írja felül.

    `market_state`: GENERIKUS piac-állapot kód. **-1 = NINCS piac-sáv** (a piac-viz
    kikapcsolva vagy nincs kiválasztott piac-stratégia) → a TradeForgeBands NEM
    rajzol piac-sávot, és 3-sávos elrendezésre vált. **0..8 = besorolás-kód** (0 =
    besorolatlan). A KERET (a per-pár piac-stratégia) tölti fel — jelenleg a
    `core.regime` osztályozó kódjával, de bármely más piac-osztályozó ugyanebbe a
    mezőbe/sávba írhat. A színt a TradeForgeBands indikátor rendeli a kódhoz."""
    t: int
    notrade: int = 0
    dir: int = 0
    window: int = 0
    market_state: int = -1

    def line(self) -> str:
        return ";".join([
            "STATE", str(int(self.t)), str(int(self.notrade)),
            str(int(self.dir)), str(int(self.window)),
            str(int(self.market_state)),
        ])


@dataclass
class Indicator:
    """A stratégia által HASZNÁLT indikátor leírása — az indikátor (MQL5) a chartra
    rakja (ChartIndicatorAdd). Nem rajz-objektum: az indikátor külön kezeli.

    kind: "MA" | "WPR" ; timeframe: "M1"/"M15"/… ; period: egész ;
    levels: WPR jelentős szintek (extrém/trigger) — az al-ablakba vízszintes
    vonalként kerülnek. Az MA-hoz üres."""
    kind: str
    timeframe: str
    period: int
    levels: tuple = ()
    color: str = ""        # vonalszín (szemantikus név); "" = MT5 alapértelmezett

    def line(self) -> str:
        col = _rgb(self.color) if self.color else "-"
        parts = ["IND", self.kind, self.timeframe, str(int(self.period)), col]
        parts += [repr(float(x)) for x in self.levels]
        return ";".join(parts)


@dataclass
class Alert:
    """RIASZTÁS az MQL5 `Alert()`-en keresztül (nem rajz-objektum).

    A „csak jelzés" módú stratégia ezzel szól, hogy MOST kellene belépni — valódi
    megbízás nélkül (lásd `core.trade_mode`). Mivel a viz-fájl a kívánt állapot
    teljes PILLANATKÉPE (minden ciklusban újraíródik), a sor önmagában ismétlődne;
    ezért van `aid` (alert-id): az indikátor MEGJEGYZI az utoljára lefuttatottat, és
    csak ÚJ id-re riaszt. Az id-t úgy kell képezni, hogy egy jelre stabil legyen
    (pl. szimbólum+irány+gyertyaidő) — így pontosan egyszer szól.

    A sor a STATE/IND-hez hasonlóan névtelen: a stratégia-tag a TÍPUS UTÁN áll
    (`tag_line`), hogy az indikátor `InpStrategy` szerint szűrhessen."""
    aid: str
    text: str

    def line(self) -> str:
        return ";".join(["ALERT", _clean(self.aid), _clean(self.text)])


@dataclass
class Label:
    """Chart-SAROKHOZ pinnelt szöveg (pixel-koordináta, nem mozog az árral).
    Pl. a beállítás-táblázat. corner: 0=bal-fent, 1=jobb-fent, 2=bal-lent,
    3=jobb-lent. x/y: távolság a saroktól pixelben."""
    name: str
    text: str
    corner: int = 0
    x: int = 10
    y: int = 20
    color: str = "white"
    fontsize: int = 9

    def line(self) -> str:
        return ";".join([
            "LABEL", _name(self.name),
            str(int(self.corner)), str(int(self.x)), str(int(self.y)),
            _rgb(self.color), str(int(self.fontsize)), _clean(self.text),
        ])
