---
name: new-strategy
description: Checklist és buktatók egy ÚJ kereskedési stratégia bevezetéséhez a TradeForge kódbázisba (strategy/ csomag). Használd, amikor új stratégiát adnál a motorhoz / dashboardhoz — "új stratégia", "add strategy", "introduce a strategy", "stratégia bevezetése", stratégia-modul, param_space, bt_entry.
---

# Új stratégia bevezetése (TradeForge)

A dashboard "váza" (megjelenítés, optimalizálás, futtatás, MT5, portfólió-backtest)
**stratégia-független**. Egy stratégia a `strategy/` csomagon át csatlakozik, a
`Strategy` interfészen ([strategy/base.py](../../strategy/base.py)) keresztül. Ez a
skill a bevezetés lépéseit ÉS a nehezen tanult buktatókat foglalja össze — kövesd
végig, mielőtt "kész"-nek jelölsz egy új stratégiát.

## 1. A stratégia-modul (`strategy/<name>.py`)

Implementáld a `Strategy` interfészt. A **kötelező** (abstract) metódusok:

| Metódus | Feladat |
|---------|---------|
| `timeframes()` | mely időkeretek (adatletöltés + visszaszámlálók); konvenció: `[0]` = magasabb tf, `[1]` = alsó tf |
| `columns()` | a stratégia dashboard-oszlopai (marker/countdown is) |
| `warmup_bars(params, tf)` | indikátor-bemelegítés gyertyaszáma |
| `compute_display(md)` | a cellák MEGJELENÍTÉSHEZ (formálódó gyertyát is használhat) |
| `new_signal_state(symbol)` / `on_bar_close(state, md)` | **élő** jelzéslogika ZÁRT gyertyán → `(state, "BUY"/"SELL"/"NONE")` |
| `base_params(cfg)` / `param_space(cfg, base, method, max_trials)` | optimalizáláshoz |

**Backtest-hookok** (a `trading.backtest` motor ezeken kéri az indikátort, jelet és
pozíciótervet — szoros ciklusban, precomputed sorokon): `bt_indicators`, `bt_warmup`,
`bt_new_state`, `bt_on_high_close`, `bt_on_low_close`, `sl_tp_pips`, `bt_entry`.

Opcionális, de gyakran kell: `signal_warmup_bars`, `live_cells`, `visual_lookback_bars`
+ `visual_objects` (MT5-viz), `grade`, `magic`, `constraints_ok`.

Minta a bevált stratégiákból: [strategy/wpr_sma.py](../../strategy/wpr_sma.py) (klasszikus),
[strategy/ml_ai.py](../../strategy/ml_ai.py) (tanítható — `fit`).

## 2. Regisztráció — AUTOMATIKUS (nincs teendő)

A `strategy/__init__.py` **auto-felderíti** a `strategy/` csomag moduljait, és a `Strategy`
interfészt implementáló osztályt a `.name` attribútuma alapján magától regisztrálja.
**Új stratégia = csak egy új modul a `strategy/`-ben** — a vázat (`__init__.py`) NEM kell
szerkeszteni. A `name` osztály-attribútum legyen EGYEDI (ez a registry-kulcs). A be nem
tölthető modult a felderítés kihagyja (warning a logban).

## 3. Elérhetőség és konfiguráció

- **`available_strategies`** (config.json): a program által felkínált stratégiák
  whitelistje. Kihagyva = az összes regisztrált. A dashboardon a **⚙ Beállítás**
  ablakból is állítható (oszlop-változás újraindítás után látszik).
- **`strategy.name`** (config.json): az ALAPÉRTELMEZETT stratégia — ezt használja egy
  pár, ha nincs saját `pairs.<sym>.strategies` listája. Ha nincs az elérhetők között,
  az elsőre esik vissza.
- **`pairs.<sym>.strategies`**: a tényleges per-instrumentum engedélyezés (több is).
- **Stratégia-config fájl**: `strategy/config/<name>.json` — `indicators`, `sltp`,
  `position_mgmt`, `quality`, és az optimalizáló-tér + `constraints`. A váz-config ezt
  betöltéskor beolvasztja (`apply_strategy_config`), mentéskor kiszűri
  (`main_config_view`) — a config.json nem szennyeződik stratégia-szekciókkal.

## 4. Kritikus buktatók (ezeken bukott már el korábbi stratégia)

- **Live↔backtest paritás:** a belépés-szűrőt ÉS a méretezést a `bt_entry` adja — a
  **live_trader és a backtest UGYANEZT hívja**. Ha máshol szűrsz/méretezel, az élő
  eredmény eltér az optimalizálttól. A motor stratégia-független (nem ismer 'atr'-t).
- **M15 look-ahead / jövő-szivárgás:** a jel CSAK zárt gyertyákból számoljon. Az ml_ai
  portnál kiderült egy motor-szintű M15 look-ahead — ellenőrizd, hogy a magasabb tf
  aktuális sora ne tartalmazzon jövőbeli információt (train/test szeletelésnél is).
- **`signal_warmup_bars` mélység:** ha a jel állapotgépe a teljes előzménytől függ (egy
  régi extrém élesít egy "jó zónát"), a live/dashboard sekély warmupja ELTÉRHET a viz
  mély ablakától → kimaradó belépések. Add meg a mély `signal_warmup_bars`-t (a viz
  `visual_lookback_bars`-ával egyező ablakállapotért).
- **M1 belépő állapotgép:** ne "szomszédos gyertyás" átütést várj (a fokozatos áttörést
  kihagyja, a BUY ~sosem tüzel). Használj **felfegyverez → tüzel** mintát (mint az M15).
  Új stratégia után **ÚJRAOPTIMALIZÁLÁS** kell.
- **Költség-tudatos breakeven:** a BE-puffernek fedeznie kell a jutalék+swapot, különben
  nettó mínusz (kül. gold/risky). A backtest NEM modellez költséget — élőben ellenőrizd.
- **Deklaratív param-kényszerek:** a `constraints_ok`-ot vezéreld configból
  (`optimizer.constraints` + range gt/lt), hogy az optuna dinamikus tartománya 0
  elpazarolt trialt adjon. Biztonságos eval: [core/param_constraints.py](../../core/param_constraints.py).
- **Egyedi magic több stratégiánál:** a `magic(cfg)`-ban adj EGYEDI magicet (pl.
  `broker.magic + eltolás`), hogy a nyitott pozíciók broker-szinten szétválaszthatók
  legyenek a stratégiák között.
- **Restart-biztos állapot:** a Play/Stop és a megszakadt optimalizálás per-(symbol,
  strategy) perzisztál (run_state, unfinished_studies → auto-folytat). A study az optuna
  SQLite-ban van (folytatható); a "friss vs. folytatás" a done/stop marker.
- **Viz upsert:** a Python nem rajzol — fájlt ír, az MQL5 indikátor (TradeForgeViz)
  upsertel (nincs törlés). Új rajz-primitívnél MT5-recompile kell.

## 5. Kapcsolódó modulok (ha a stratégia használja)

- Kiszállási jel: [core/exit_signal.py](../../core/exit_signal.py) (RUNNER_EXIT — runner
  zárása indikátor-jelre).
- Pozícióépítés: [core/position_build.py](../../core/position_build.py) (piramidális add +
  átlagár-stop).
- Kockázatcsökkentés (Felező/Pajzs/Risky), piac-előszűrő (market_strategy) — per-pár.

## 6. Ellenőrzés (mielőtt "kész")

1. `python -m py_compile strategy/<name>.py` és a modul importja hibátlan.
2. `available_strategy_names(cfg)` / a per-pár választó felkínálja; oszlop megjelenik
   (újraindítás után).
3. Optimalizálás lefut (Opt gomb; tanítható stratégiánál = tanítás), 0 érvénytelen trial
   a constraints-tól; done-marker + "Utolsó opt" dátum megjelenik.
4. Backtest ↔ live paritás: ugyanaz a `bt_entry`-terv élőben és backtestben.
5. Egy portfólió-backtest a stratégiával; a P&L/R értelmes.
