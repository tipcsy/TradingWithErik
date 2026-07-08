"""
Központi alkalmazás-metaadat: NÉV és VERZIÓ.

Egy helyen definiálva, hogy az ablakcím, a fejléc-kijelzés és a jövőbeli
EXE-build (PyInstaller) is ugyanazt használja. Verzióemeléskor CSAK ezt kell átírni.

Verziószámozás (Semantic Versioning): MAJOR.MINOR.PATCH
  - MAJOR: nagy, visszafelé nem kompatibilis változás
  - MINOR: új funkció, kompatibilis
  - PATCH: hibajavítás
"""

APP_NAME    = "TradeForge"
APP_VERSION = "1.4.0"
APP_TITLE   = f"{APP_NAME} v{APP_VERSION}"


# ---------------------------------------------------------------------------
# Futásidejű alap-könyvtár (fejlesztés ↔ PyInstaller EXE egységesen)
# ---------------------------------------------------------------------------
# Fejlesztésben: a projekt gyökere (ez a fájl mellett).
# EXE-ben (sys.frozen): az .exe mappája — így a config.json és a data/ az .exe
# mellől olvasódik. Build-napon a modulok data/config elérése erre cserélhető.
import sys as _sys
from pathlib import Path as _Path

if getattr(_sys, "frozen", False):
    BASE_DIR = _Path(_sys.executable).resolve().parent
else:
    BASE_DIR = _Path(__file__).resolve().parent
