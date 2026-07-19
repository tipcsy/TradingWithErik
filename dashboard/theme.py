"""
Központi színpaletta + betűtípus + szemantikus szín-leképezés.

A stratégia modulok NEM ismerik a konkrét hex kódokat — csak szemantikus
neveket adnak vissza (pl. "green", "red", "muted"), így a megjelenítés
(tkinter) cseréje vagy témázása egy helyen történik.

TÉMÁK
-----
A paletta NEVESÍTETT TÉMÁKBÓL jön (`THEMES`). Egy téma a hátteret ÉS a
betűszíneket EGYÜTT hozza — ezért szerkezetileg lehetetlen a „fehér alapon fehér
szöveg”: nincs olyan állapot, ahol a kettőt külön lehetne elállítani.

A választott téma a `config.json`-ban él (`dashboard.theme`), és ez a modul
IMPORT-IDŐBEN olvassa be. Ennek oka: a többi modul `from dashboard.theme import
BG, FG_WHITE, …` formában, ÉRTÉK SZERINT köti a neveket, így a futásidejű
átírás nem propagálna. Ezért a téma-váltás a KÖVETKEZŐ INDÍTÁSKOR jelenik meg
(a beállító ablak ezt ki is írja).

BETŰTÍPUS
---------
A betűcsalád és az alapméret szintén configból jön (`dashboard.font_family` /
`dashboard.font_size`), de az AZONNAL érvényesül: a `fonts()` MEGOSZTOTT
`tkfont.Font` objektumokat ad szerepenként (`FONT_ROLES`), és a tkinter minden
widgetje élőben követi ezek `configure()`-ját — az `apply_fonts()` egy hívással
átállítja mindet, minden GUI-modulban.
"""

from __future__ import annotations

import json
import logging

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Témák — minden téma UGYANAZOKAT a kulcsokat adja meg (lásd _KEYS).
# ---------------------------------------------------------------------------

# Sötét, alacsony kontrasztú alap (Catppuccin Mocha) — a program eddigi kinézete.
_MOCHA = {
    "BG":           "#1e1e2e",
    "BG_HEADER":    "#181825",
    "BG_ROW_ODD":   "#1e1e2e",
    "BG_ROW_EVEN":  "#242438",
    "BG_INACTIVE":  "#2a2a3e",
    "BG_UNTRAINED": "#222230",
    "BG_OPT_ROW":   "#2a2a1e",
    "BG_BT":        "#1a1a2e",

    "FG_WHITE":     "#cdd6f4",
    "FG_GREEN":     "#a6e3a1",
    "FG_RED":       "#f38ba8",
    "FG_YELLOW":    "#f9e2af",
    "FG_GRAY":      "#585b70",
    "FG_GRAY_DIM":  "#45475a",
    "FG_BLUE":      "#89b4fa",
    "FG_CYAN":      "#89dceb",
    "FG_ORANGE":    "#fab387",
    "FG_PURPLE":    "#cba6f7",
    "FG_TEAL":      "#94e2d5",
    # Szöveg SZÍNES háttéren (gomb-felirat, kiemelt cella) — sötét témán a sötét
    # alapszín olvasható a világos akcenten.
    "FG_ON_ACCENT": "#1e1e2e",

    "BTN_PLAY_BG":  "#40a02b",
    "BTN_PLAY_FG":  "#ffffff",
    "BTN_STOP_BG":  "#d20f39",
    "BTN_STOP_FG":  "#ffffff",
    "BTN_OPT_BG":   "#7287fd",
    "BTN_OPT_FG":   "#ffffff",
    "BTN_BT_BG":    "#e64553",
    "BTN_BT_FG":    "#ffffff",
    "BTN_DIS_BG":   "#313244",
    "BTN_DIS_FG":   "#585b70",

    "CANVAS_BG":    "#11111b",
    "CANVAS_LINE":  "#a6e3a1",
    "CANVAS_REF":   "#585b70",

    "TOOLTIP_BG":   "#2a2a3a",
    "TOOLTIP_FG":   "#e0e0f0",
}

# Világos téma — a HÁTTÉR világos, ezért a betűszínek SÖTÉTEK. (A szemantikus
# zöld/piros is sötétebb árnyalat, hogy fehéren is olvasható legyen: a sötét
# témák pasztell színei világos alapon eltűnnének.)
_LIGHT = {
    "BG":           "#eff1f5",
    "BG_HEADER":    "#dce0e8",
    "BG_ROW_ODD":   "#eff1f5",
    "BG_ROW_EVEN":  "#e6e9ef",
    "BG_INACTIVE":  "#dcdfe6",
    "BG_UNTRAINED": "#e4e6ec",
    "BG_OPT_ROW":   "#f2eede",
    "BG_BT":        "#e9ebf0",

    "FG_WHITE":     "#4c4f69",   # az „alap szövegszín" — világos témán SÖTÉT
    "FG_GREEN":     "#2f7d21",
    "FG_RED":       "#d20f39",
    "FG_YELLOW":    "#a06d00",   # sárga fehéren olvashatatlan → sötét okker
    "FG_GRAY":      "#8c8fa1",
    "FG_GRAY_DIM":  "#acb0be",
    "FG_BLUE":      "#1e66f5",
    "FG_CYAN":      "#0f7076",
    "FG_ORANGE":    "#b05708",
    "FG_PURPLE":    "#8839ef",
    "FG_TEAL":      "#0f7076",
    "FG_ON_ACCENT": "#ffffff",   # világos témán a színes gombokra FEHÉR felirat

    "BTN_PLAY_BG":  "#40a02b",
    "BTN_PLAY_FG":  "#ffffff",
    "BTN_STOP_BG":  "#d20f39",
    "BTN_STOP_FG":  "#ffffff",
    "BTN_OPT_BG":   "#1e66f5",
    "BTN_OPT_FG":   "#ffffff",
    "BTN_BT_BG":    "#e64553",
    "BTN_BT_FG":    "#ffffff",
    "BTN_DIS_BG":   "#ccd0da",
    "BTN_DIS_FG":   "#8c8fa1",

    "CANVAS_BG":    "#ffffff",
    "CANVAS_LINE":  "#40a02b",
    "CANVAS_REF":   "#acb0be",

    "TOOLTIP_BG":   "#4c4f69",
    "TOOLTIP_FG":   "#eff1f5",
}

# Magas kontraszt (sötét) — tiszta fekete alap, élénk színek. Nagy monitorra /
# gyenge fényviszonyokra, ahol a Mocha pasztelljei elmosódnak.
_CONTRAST = {
    "BG":           "#000000",
    "BG_HEADER":    "#0a0a0a",
    "BG_ROW_ODD":   "#000000",
    "BG_ROW_EVEN":  "#141414",
    "BG_INACTIVE":  "#1c1c1c",
    "BG_UNTRAINED": "#121212",
    "BG_OPT_ROW":   "#1c1c00",
    "BG_BT":        "#0a0a0a",

    "FG_WHITE":     "#ffffff",
    "FG_GREEN":     "#00ff5f",
    "FG_RED":       "#ff2d55",
    "FG_YELLOW":    "#ffd700",
    "FG_GRAY":      "#9e9e9e",
    "FG_GRAY_DIM":  "#6e6e6e",
    "FG_BLUE":      "#4da6ff",
    "FG_CYAN":      "#00e5ff",
    "FG_ORANGE":    "#ff9f0a",
    "FG_PURPLE":    "#bf5aff",
    "FG_TEAL":      "#00e0c0",
    "FG_ON_ACCENT": "#000000",

    "BTN_PLAY_BG":  "#00c853",
    "BTN_PLAY_FG":  "#000000",
    "BTN_STOP_BG":  "#ff1744",
    "BTN_STOP_FG":  "#ffffff",
    "BTN_OPT_BG":   "#2979ff",
    "BTN_OPT_FG":   "#ffffff",
    "BTN_BT_BG":    "#ff5252",
    "BTN_BT_FG":    "#000000",
    "BTN_DIS_BG":   "#2a2a2a",
    "BTN_DIS_FG":   "#8a8a8a",

    "CANVAS_BG":    "#000000",
    "CANVAS_LINE":  "#00ff5f",
    "CANVAS_REF":   "#6e6e6e",

    "TOOLTIP_BG":   "#1c1c1c",
    "TOOLTIP_FG":   "#ffffff",
}

# Megjelenítendő név → paletta. A sorrend a beállító ablak sorrendje.
THEMES: dict[str, dict] = {
    "Sötét (Mocha)":   _MOCHA,
    "Világos":         _LIGHT,
    "Magas kontraszt": _CONTRAST,
}
DEFAULT_THEME = "Sötét (Mocha)"

# A kötelező kulcsok halmaza — minden témának teljesnek kell lennie (a hiányt a
# betöltés a Mocha értékével pótolja, hogy soha ne legyen None egy widget-színben).
_KEYS = tuple(_MOCHA.keys())

# Betűtípus alapértékek. A szerepenkénti méretek az alapméretből SZÁRMAZNAK
# (lásd `FONT_ROLES` + `fonts`), így egy szám állításával az egész felület
# arányosan nő/csökken.
DEFAULT_FONT_FAMILY = "Courier New"
DEFAULT_FONT_SIZE   = 9
FONT_SIZE_MIN, FONT_SIZE_MAX = 6, 20

# Ajánlott családok a legördülőhöz (a rendszeren elérhetőkre szűrve jelenik meg).
FONT_FAMILIES = ["Courier New", "Consolas", "Cascadia Mono", "Lucida Console",
                 "Segoe UI", "Arial", "Tahoma", "Verdana"]


# ---------------------------------------------------------------------------
# Beállítás-betöltés (config.json → aktív téma + betű)
# ---------------------------------------------------------------------------

def _config_path():
    """A config.json útvonala. Külön olvassuk (nem a strategy.settings loaderen
    át), mert ez a modul a GUI-nál KORÁBBAN töltődik be, és csak a `dashboard`
    szekció kell belőle — így nincs körkörös import."""
    from version import BASE_DIR
    return BASE_DIR / "config.json"


def load_prefs() -> dict:
    """A `dashboard` megjelenítési beállításai a config.json-ból:
    `{"theme": <név>, "font_family": <név>, "font_size": <int>}`.
    Hiányzó/hibás fájl esetén az alapértékek (a GUI sosem áll meg emiatt)."""
    theme, family, size = DEFAULT_THEME, DEFAULT_FONT_FAMILY, DEFAULT_FONT_SIZE
    try:
        with open(_config_path(), encoding="utf-8") as f:
            dash = (json.load(f).get("dashboard") or {})
        if dash.get("theme") in THEMES:
            theme = dash["theme"]
        if isinstance(dash.get("font_family"), str) and dash["font_family"].strip():
            family = dash["font_family"].strip()
        _s = dash.get("font_size")
        if isinstance(_s, (int, float)) and FONT_SIZE_MIN <= _s <= FONT_SIZE_MAX:
            size = int(_s)
    except Exception as ex:
        log.debug("Megjelenítési beállítás nem olvasható (alapértékek): %s", ex)
    return {"theme": theme, "font_family": family, "font_size": size}


_PREFS      = load_prefs()
ACTIVE_THEME = _PREFS["theme"]
FONT_FAMILY  = _PREFS["font_family"]
FONT_SIZE    = _PREFS["font_size"]

# Az aktív paletta — hiányzó kulcsot a Mocha pótol (védelem a hibás témától).
_P = {**_MOCHA, **THEMES.get(ACTIVE_THEME, _MOCHA)}


# --- Nyers paletta (a modul-szintű nevek: `from dashboard.theme import BG, …`) --
BG           = _P["BG"]
BG_HEADER    = _P["BG_HEADER"]
BG_ROW_ODD   = _P["BG_ROW_ODD"]
BG_ROW_EVEN  = _P["BG_ROW_EVEN"]
BG_INACTIVE  = _P["BG_INACTIVE"]
BG_UNTRAINED = _P["BG_UNTRAINED"]
BG_OPT_ROW   = _P["BG_OPT_ROW"]
BG_BT        = _P["BG_BT"]

FG_WHITE     = _P["FG_WHITE"]
FG_GREEN     = _P["FG_GREEN"]
FG_RED       = _P["FG_RED"]
FG_YELLOW    = _P["FG_YELLOW"]
FG_GRAY      = _P["FG_GRAY"]
FG_GRAY_DIM  = _P["FG_GRAY_DIM"]
FG_BLUE      = _P["FG_BLUE"]
FG_CYAN      = _P["FG_CYAN"]
FG_ORANGE    = _P["FG_ORANGE"]
FG_PURPLE    = _P["FG_PURPLE"]
FG_TEAL      = _P["FG_TEAL"]
FG_ON_ACCENT = _P["FG_ON_ACCENT"]

BTN_PLAY_BG  = _P["BTN_PLAY_BG"]
BTN_PLAY_FG  = _P["BTN_PLAY_FG"]
BTN_STOP_BG  = _P["BTN_STOP_BG"]
BTN_STOP_FG  = _P["BTN_STOP_FG"]
BTN_OPT_BG   = _P["BTN_OPT_BG"]
BTN_OPT_FG   = _P["BTN_OPT_FG"]
BTN_BT_BG    = _P["BTN_BT_BG"]
BTN_BT_FG    = _P["BTN_BT_FG"]
BTN_DIS_BG   = _P["BTN_DIS_BG"]
BTN_DIS_FG   = _P["BTN_DIS_FG"]

CANVAS_BG    = _P["CANVAS_BG"]
CANVAS_LINE  = _P["CANVAS_LINE"]
CANVAS_REF   = _P["CANVAS_REF"]

TOOLTIP_BG   = _P["TOOLTIP_BG"]
TOOLTIP_FG   = _P["TOOLTIP_FG"]


# --- Szemantikus nevek → hex ----------------------------------------------
# A stratégia cellák (és a váz oszlopai) ezeket a neveket adják vissza.
SEMANTIC = {
    "up":      FG_GREEN,    # növekvő ár / pozitív
    "down":    FG_RED,      # csökkenő ár / negatív
    "neutral": FG_WHITE,    # nincs változás
    "green":   FG_GREEN,
    "red":     FG_RED,
    "yellow":  FG_YELLOW,
    "white":   FG_WHITE,
    "muted":   FG_GRAY,
    "dim":     FG_GRAY_DIM,
    "blue":    FG_BLUE,
    "cyan":    FG_CYAN,
    "orange":  FG_ORANGE,
}


def color(name: str) -> str:
    """Szemantikus szín-név → hex. Ismeretlen név esetén az alap szövegszín."""
    return SEMANTIC.get(name, FG_WHITE)


# ---------------------------------------------------------------------------
# Betűtípusok
# ---------------------------------------------------------------------------

# Szerep → (méret-eltolás az alapmérethez képest, félkövér, dőlt).
FONT_ROLES = {
    "mono":        (0,  False, False),   # táblázat-cellák
    "mono_bold":   (0,  True,  False),   # kiemelt cella (pl. LIVE instrumentum-név)
    "mono_italic": (0,  False, True),    # halvány/offline állapotú cella
    "header":      (0,  True,  False),   # oszlopfejlécek, ablak-címek
    "small":       (-1, False, False),   # vezérlők, súgó-szövegek
    "small_bold":  (-1, True,  False),   # kiemelt vezérlő-felirat
    "title":       (+1, True,  False),   # fő cím
    "info":        (0,  False, False),   # státusz-sorok
    "tiny":        (-2, False, False),   # diagram-tengely feliratok
}

# A MEGOSZTOTT betű-objektumok. Szándékosan modul-szintű szingleton: több GUI-modul
# (gui, instrument_dialog, backtest_dialog) használja őket, és a beállító ablak
# EGY `apply_fonts()` hívással mindet átállítja. Ha modulonként külön objektumok
# lennének, a méret-váltás csak a fő ablakon látszana.
_FONTS: dict = {}


def fonts() -> dict:
    """A megosztott betű-objektumok szerepenként (`FONT_ROLES`), lazán létrehozva.

    `tkfont.Font` objektumokat ad, nem tuple-öket: ezek ÉLŐK — a `configure()`
    minden widgeten azonnal átüt, amelyik használja őket. Ezért azonnali a
    betűváltás, míg a szín-váltás újraindítást igényel.

    Tk-gyökér kell hozzá → a hívó a `tk.Tk()` UTÁN hívja."""
    if not _FONTS:
        from tkinter import font as tkfont
        for role, (delta, bold, italic) in FONT_ROLES.items():
            _FONTS[role] = tkfont.Font(
                family=FONT_FAMILY, size=max(FONT_SIZE_MIN, FONT_SIZE + delta),
                weight="bold" if bold else "normal",
                slant="italic" if italic else "roman")
    return _FONTS


def apply_fonts(family: str, size: int) -> None:
    """A megosztott betű-objektumok ÁTÁLLÍTÁSA — a felület azonnal követi.
    A szerepenkénti méret-eltolás/stílus ugyanúgy érvényesül, mint létrehozáskor."""
    base = max(FONT_SIZE_MIN, min(FONT_SIZE_MAX, int(size)))
    for role, f in fonts().items():
        delta, bold, italic = FONT_ROLES.get(role, (0, False, False))
        f.configure(family=family, size=max(FONT_SIZE_MIN, base + delta),
                    weight="bold" if bold else "normal",
                    slant="italic" if italic else "roman")
