# MT5 backtest-reprodukció (BacktestReplayer)

A Python-backtest belépőit az MT5 Strategy Testerben lehet reprodukálni.

## Menete
1. **Futtass egy backtestet** a felületen (instrumentum-ablak → Backtest). A végén a
   belépők automatikusan CSV-be íródnak:
   `data/mt5_backtest/mt5_backtest_<SYMBOL>_<időbélyeg>.csv`
   (az eredmény-sáv kiírja a fájlnevet).
2. **Másold a CSV-t** az MT5 közös mappájába:
   `<Terminál>\Common\Files\` (a `BacktestReplayer.mq5` induláskor ír egy
   `ide_kell_helyezni.txt`-t a pontos útvonallal).
3. **Fordítsd le** a `tools/BacktestReplayer.mq5`-öt a MetaEditorban (Experts közé).
4. **Strategy Tester**: az adott `<SYMBOL>` **M1** időkeret, „Csak nyitóárak" (Open
   prices only) ajánlott. Add hozzá a `BacktestReplayer` expertet, az inputoknál
   állítsd:
   - `InpCsvFile` = a CSV fájlneve,
   - `InpPipSize` = a szimbólum pip-mérete,
   - `InpMagic` = tetszőleges egyedi szám.
5. Indítsd — az EA az OPEN-eseményekből nyit, a BE/trail/SL/TP-t **belül** kezeli
   (a CSV `be_trigger`/`trail_trigger`/`trail_dist_p` alapján, M1 bar H→L logikával),
   és kirajzolja a trade-eket.

## Fontos / korlátok
- **Trading-with-Erik M1-belépőket ad** → az EA **M1**-en fut (a Trading-with-ai
  M15-öt adott). Az OPEN időbélyege a belépő + 1 M1 (bar-záró).
- A modell az **OFF preset** (egyszerű BE + trail) logikájával egyezik. A
  risky/felező/pajzs preset, a kiszállási jel és a pozícióépítés a **Python-oldalon**
  van, az EA-ban nincs modellezve → azoknál az MT5-eredmény eltérhet.
- A CSV a `tools/mt5_export.py`-ból jön (12 oszlop: event, datetime, symbol,
  direction, price, sl, tp, lot, comment, be_trigger, trail_trigger, trail_dist_p).
- Forrás: áthozva a `Trading-with-ai` projektből (ml_backtest.py + BacktestReplayer.mq5).
