@echo off
REM ===========================================================================
REM  TradeForge — EXE build (PyInstaller)
REM  A projekt GYÖKERÉBŐL futtasd:  build\build.bat
REM ===========================================================================

echo [TradeForge] PyInstaller telepitese (ha meg nincs)...
python -m pip install --upgrade pyinstaller

echo [TradeForge] Regi build torlese...
if exist build\TradeForge rmdir /s /q build\TradeForge
if exist dist\TradeForge  rmdir /s /q dist\TradeForge

echo [TradeForge] Build indul...
python -m PyInstaller build\TradeForge.spec --noconfirm

echo.
echo [TradeForge] KESZ.  Kimenet: dist\TradeForge\TradeForge.exe
echo   -> Masold a config.json fajlt es a data\ mappat a TradeForge.exe melle!
pause
