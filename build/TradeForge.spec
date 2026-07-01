# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec — TradeForge

Build (a projekt GYÖKERÉBŐL futtatva):
    python -m PyInstaller build/TradeForge.spec

Kimenet: dist/TradeForge/TradeForge.exe  (onedir — mellé kerül a config.json és data/)

FONTOS — build-napi teendők (lásd build/README.md):
  1) A modulok data/config elérését a version.BASE_DIR-re kell állítani, hogy az
     EXE az .exe mellől olvasson (fejlesztésben ez a projektgyökér, tehát nem tör el).
  2) Ikon: tedd az assets/tradeforge.ico fájlt a helyére (különben ikon nélkül épül).
"""

from pathlib import Path

PROJECT_ROOT = Path(SPECPATH).resolve().parent      # a spec a build/ mappában van
ICON = PROJECT_ROOT / "assets" / "tradeforge.ico"

block_cipher = None

a = Analysis(
    [str(PROJECT_ROOT / "main.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=[],                       # config.json / data/ NEM beépítve — az .exe mellé kerül
    hiddenimports=[
        "MetaTrader5", "optuna", "pandas", "numpy",
        "pyarrow", "fastparquet",
        "tkinter", "tkinter.ttk", "tkinter.font",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="TradeForge",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,          # 1.0: True = látszik a log/hiba. Kész release-nél False (nincs konzolablak).
    disable_windowed_traceback=False,
    icon=str(ICON) if ICON.exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="TradeForge",
)
