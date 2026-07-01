# TradeForge — EXE build útmutató

> **Állapot:** a build-script **készen áll**, de a tényleges EXE-t szándékosan
> **csak a kész (stabil) verziónál** építjük meg. Amíg aktív hibajavítás zajlik,
> felesleges minden körben újrabuildelni.

## Verziózás

A név és a verzió **egy helyen** van: [`version.py`](../version.py)

```python
APP_NAME    = "TradeForge"
APP_VERSION = "1.0.0"
```

- A verzió **látható** a felület felső sávjában (`v1.0.0`, cián színnel a név mellett)
  és az ablak címsorában is.
- Új kiadáskor **csak a `version.py`-t** kell átírni (SemVer: MAJOR.MINOR.PATCH).

## Build lépések (ha eljön az idő)

1. **Ikon** (opcionális, de ajánlott): tedd az `assets/tradeforge.ico` fájlt a helyére.
   Ha nincs, ikon nélkül épül.
2. **Elérési utak frozen módra** (egyszeri kód-lépés): a modulok jelenleg saját
   `ROOT = Path(__file__).resolve().parents[N]` alapján olvassák a `config.json`-t és
   a `data/` mappát. EXE-ben ez a csomag belső könyvtárára mutat, nem az `.exe` mellé.
   Cseréld a **config/data olvasás** bázisát a `version.BASE_DIR`-re:

   ```python
   from version import BASE_DIR      # fejlesztésben = projektgyökér, EXE-ben = az .exe mappája
   CFG_PATH = BASE_DIR / "config.json"
   ```

   Érintett fájlok: `main.py`, `dashboard/gui.py`, `trading/live_trader.py`,
   `ml/optimizer.py`, `tools/download_history.py`, `core/risky_mode.py`,
   `core/correlation.py` (mindegyikben a `data/` és `config.json` elérés).
   Fejlesztésben a viselkedés **nem változik** (BASE_DIR ugyanaz a gyökér).
3. **Build futtatása** a projekt gyökeréből:

   ```
   build\build.bat
   ```

   vagy kézzel:

   ```
   python -m PyInstaller build\TradeForge.spec --noconfirm
   ```
4. **Kimenet:** `dist\TradeForge\TradeForge.exe` (onedir).
   A `config.json`-t és a `data\` mappát **másold az `.exe` mellé**.

## Megjegyzések

- `console=True` a spec-ben (1.0): a hiba- és log-üzenetek látszanak egy konzolablakban.
  Kész, letisztult kiadásnál állítsd `console=False`-ra (nincs konzolablak).
- Az `MetaTrader5` csomag csak Windowson érhető el → az EXE is Windows-only.
- A `pip` csomagok, amiket a build igényel: `pyinstaller` (a `build.bat` telepíti).
