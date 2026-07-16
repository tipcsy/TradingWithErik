"""
Központi színpaletta + szemantikus szín-leképezés.

A stratégia modulok NEM ismerik a konkrét hex kódokat — csak szemantikus
neveket adnak vissza (pl. "green", "red", "muted"), így a megjelenítés
(tkinter) cseréje vagy témázása egy helyen történik.
"""

# --- Nyers paletta (Catppuccin Mocha) -------------------------------------
BG           = "#1e1e2e"
BG_HEADER    = "#181825"
BG_ROW_ODD   = "#1e1e2e"
BG_ROW_EVEN  = "#242438"
BG_INACTIVE  = "#2a2a3e"
BG_UNTRAINED = "#222230"
BG_OPT_ROW   = "#2a2a1e"
BG_BT        = "#1a1a2e"

FG_WHITE     = "#cdd6f4"
FG_GREEN     = "#a6e3a1"
FG_RED       = "#f38ba8"
FG_YELLOW    = "#f9e2af"
FG_GRAY      = "#585b70"
FG_GRAY_DIM  = "#45475a"
FG_BLUE      = "#89b4fa"
FG_CYAN      = "#89dceb"
FG_ORANGE    = "#fab387"
FG_PURPLE    = "#cba6f7"
FG_TEAL      = "#94e2d5"

BTN_PLAY_BG  = "#40a02b"
BTN_PLAY_FG  = "#ffffff"
BTN_STOP_BG  = "#d20f39"
BTN_STOP_FG  = "#ffffff"
BTN_OPT_BG   = "#7287fd"
BTN_OPT_FG   = "#ffffff"
BTN_BT_BG    = "#e64553"
BTN_BT_FG    = "#ffffff"
BTN_DIS_BG   = "#313244"
BTN_DIS_FG   = "#585b70"

CANVAS_BG    = "#11111b"
CANVAS_LINE  = "#a6e3a1"
CANVAS_REF   = "#585b70"


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
    """Szemantikus szín-név → hex. Ismeretlen név esetén fehér."""
    return SEMANTIC.get(name, FG_WHITE)
