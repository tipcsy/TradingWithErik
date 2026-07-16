"""
ML-AI stratégia — a Trading-with-AI projekt gépi tanulásos belépő-jelzése a
Strategy seam mögé csomagolva.

Működés: páronként és irányonként (long/short) egy tanított osztályozó
(LightGBM/RandomForest) + StandardScaler + kalibrált valószínűség-küszöb.
Minden M15 gyertyazáráskor a ~50 feature-ből (strategy.ml_features) predikció
készül; ha P(irány) >= küszöb ÉS a session-óra engedi, a KÖVETKEZŐ M1 záráskor
belépő jel születik. Az SL/TP ATR-alapú (vagy fix pip) — a pozíciómenedzsment
(BE/trailing/kockázatcsökkentés) a keretrendszer meglévő presetjeié.

Modelltár: data/models/ml_ai/<SYMBOL>.pkl — a tanítás (Fázis 2) írja, itt csak
betöltjük (mtime-cache; újratanítás után automatikusan frissül). Modell nélkül
a stratégia némán inaktív (nincs jel).

KAUZALITÁS: a backtest-motor a formálódó M15 gyertya sorát adja a hookoknak
(lásd run_pair m15_ptr), ezért a jel-állapotgép az ELŐZŐ (már zárt) sor
predikcióját tárolja és arra tüzel — így a backtest pontosan azt látja, amit
a live: az utolsó ZÁRT gyertya jelét, a következő M1 záráskor végrehajtva.
"""

from __future__ import annotations

import logging
import math
import pickle
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

# A scaler/modell ndarray-jal dolgozik (szándékosan — a live/backtest/tanítás
# egységesen oszlopsorrend-alapú); a sklearn feature-név figyelmeztetése zaj.
warnings.filterwarnings("ignore", message="X does not have valid feature names")

from strategy.base import (
    Strategy, Column, StrategyColumn, MarkerColumn,
    MarketData, Cell, Timeframe,
)
from strategy import ml_features as mlf


# ---------------------------------------------------------------------------
# Modelltár — data/models/ml_ai/<SYMBOL>.pkl
# ---------------------------------------------------------------------------

MODELS_DIR = Path(__file__).resolve().parents[1] / "data" / "models" / "ml_ai"

# A wpr_sma a broker.magic-et használja; a több-stratégiás szétválasztáshoz az
# ML-AI pozíciói EGYEDI magicet kapnak (broker.magic + eltolás).
MAGIC_OFFSET = 1


def model_path(symbol: str) -> Path:
    return MODELS_DIR / f"{symbol}.pkl"


# (symbol) → (mtime, bundle) — újratanítás után (mtime változás) automatikus reload
_bundle_cache: dict[str, tuple[float, Optional[dict]]] = {}


def load_bundle(symbol: str) -> Optional[dict]:
    """A pár tanított modell-csomagja: {"long": {"model","scaler","threshold"},
    "short": {...}, "features": [...], "meta": {...}} — vagy None, ha nincs."""
    p = model_path(symbol)
    try:
        mtime = p.stat().st_mtime
    except OSError:
        _bundle_cache.pop(symbol, None)
        return None
    cached = _bundle_cache.get(symbol)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        with open(p, "rb") as f:
            bundle = pickle.load(f)
    except Exception as ex:
        log.warning("%s — ML modell betöltési hiba (%s): %s", symbol, p.name, ex)
        bundle = None
    _bundle_cache[symbol] = (mtime, bundle)
    return bundle


def _predict_frame(feats: pd.DataFrame, bundle: Optional[dict]) -> tuple[np.ndarray, np.ndarray]:
    """Vektoros predikció a teljes feature-frame-re: (p_long, p_short) tömbök.
    Érvénytelen (NaN feature-ös) sorok és hiányzó modell → 0.0 (sosem tüzel)."""
    n = len(feats)
    p_long  = np.zeros(n)
    p_short = np.zeros(n)
    if bundle is None or n == 0:
        return p_long, p_short
    cols = bundle.get("features", mlf.FEATURES)
    X = feats[cols].to_numpy(dtype=float)
    valid = ~np.isnan(X).any(axis=1)
    if not valid.any():
        return p_long, p_short
    for direction, out in (("long", p_long), ("short", p_short)):
        d = bundle.get(direction)
        if not d or d.get("model") is None:
            continue
        try:
            Xs = d["scaler"].transform(X[valid])
            out[valid] = d["model"].predict_proba(Xs)[:, 1]
        except Exception as ex:
            log.warning("ML predikciós hiba (%s): %s", direction, ex)
    return p_long, p_short


def _thresholds(bundle: Optional[dict]) -> tuple[float, float]:
    """(küszöb_long, küszöb_short) — hiányzó modell/irány → 1.01 (sosem tüzel)."""
    if bundle is None:
        return 1.01, 1.01
    tl = (bundle.get("long") or {}).get("threshold", 1.01)
    ts = (bundle.get("short") or {}).get("threshold", 1.01)
    return float(tl), float(ts)


def _sess_ok(hour: int, params: dict) -> bool:
    """Session-óra kapu (szerver-idő, mint a tanítás session-szűrője).
    sess_start == sess_end → nincs szűrés; átfordulós (start > end) is kezelt."""
    try:
        s = int(params.get("sess_start", 0))
        e = int(params.get("sess_end", 24))
    except (TypeError, ValueError):
        return True
    if s == e or (s <= 0 and e >= 24):
        return True
    if s < e:
        return s <= hour < e
    return hour >= s or hour < e


def _evaluate(p_long: float, p_short: float, thr_long: float, thr_short: float,
              hour: int, params: dict) -> str:
    """Egy ZÁRT gyertya predikciójából a jel: 'BUY' | 'SELL' | 'NONE'.
    Mindkét irány átüt (ritka) → a küszöbhöz képest erősebb nyer."""
    if not _sess_ok(hour, params):
        return "NONE"
    buy  = p_long  >= thr_long
    sell = p_short >= thr_short
    if buy and sell:
        return "BUY" if (p_long - thr_long) >= (p_short - thr_short) else "SELL"
    if buy:
        return "BUY"
    if sell:
        return "SELL"
    return "NONE"


# ---------------------------------------------------------------------------
# Jelzésállapotok
# ---------------------------------------------------------------------------

@dataclass
class MlAiBtState:
    """Backtest-állapot. A motor a FORMÁLÓDÓ M15 sort adja (lásd modul-docstring),
    ezért az előző sor (= utolsó ZÁRT gyertya) predikcióját tároljuk, és új sor
    érkezésekor ABBÓL képezzük a jelet → kauzális, a live-val egyező időzítés."""
    symbol: str
    last_key: Any = None          # az utoljára látott M15 sor ideje
    prev_p_long: float = 0.0      # az előző (zárt) sor predikciója
    prev_p_short: float = 0.0
    prev_hour: int = -1
    prev_valid: bool = False
    pending: str = "NONE"         # az új M15 sornál élesített jel (egyszer tüzel)


@dataclass
class MlAiState:
    """Élő jelzésállapot (a futtatómotor tartja életben páronként)."""
    symbol: str
    last_m15_time: Optional[pd.Timestamp] = None
    model_ok: bool = False
    sess_ok: bool = True
    p_long: float = float("nan")
    p_short: float = float("nan")
    thr_long: float = 1.01
    thr_short: float = 1.01
    last_signal: str = "NONE"     # az utolsó M15 zárás jele (a kijelzés latch-eli)


# ---------------------------------------------------------------------------
# Megjelenítési segédek
# ---------------------------------------------------------------------------

_CIRCLE = "●"
# Az ML-belépőnek nincs többlépcsős állapotgépe (mint a wpr_sma 3 köre): a
# releváns állapot a modell megléte és a jel. A session-kapu a jelbe van
# beszámítva (session-en kívül nem születik jel).
_STAGES = (("model", "Modell betöltve"), ("sig", "ML belépő jel"))
_MARKS_EMPTY = {k: Cell(_CIRCLE, "muted") for k, _ in _STAGES}


def _stage_bool(ok: bool, color: str = "green") -> Cell:
    return Cell(_CIRCLE, color if ok else "muted")


def _stage_signal(signal: str) -> Cell:
    if signal == "BUY":
        return Cell(_CIRCLE, "green")
    if signal == "SELL":
        return Cell(_CIRCLE, "red")
    return Cell(_CIRCLE, "muted")


def _proba_cell(p_long: float, p_short: float, thr_long: float, thr_short: float) -> Cell:
    """P(long)/P(short) egy cellában; a küszöböt átütő oldal színez."""
    if math.isnan(p_long) or math.isnan(p_short):
        return Cell("—", "muted")
    txt = f"{p_long:.2f}/{p_short:.2f}"
    if p_long >= thr_long and p_long - thr_long >= p_short - thr_short:
        return Cell(txt, "green")
    if p_short >= thr_short:
        return Cell(txt, "red")
    return Cell(txt, "white")


# ---------------------------------------------------------------------------
# A stratégia
# ---------------------------------------------------------------------------

class MlAiStrategy(Strategy):
    name = "ml_ai"

    # --- Megjelenítés -----------------------------------------------------

    def timeframes(self) -> list[Timeframe]:
        # A jel M15-ön születik, a végrehajtás (belépő ár, SL/TP szimuláció) M1-en
        # — mint a wpr_sma-nál, így az adatréteg változatlan. A H1 kontextust a
        # feature-motor az M15-ből resample-eli.
        return [Timeframe("M15", 15), Timeframe("M1", 1)]

    def columns(self) -> list[Column]:
        # Csak a körös jelölő (Modell · Session · Belépő jel). A P(long)/P(short)
        # cellát a live_cells/compute_display így is adja ("ml_proba" kulcs) —
        # a számoszlop bekötése a váz lapos cella-útvonalába későbbi kör.
        return [MarkerColumn("ml_marks", self.name, stages=_STAGES)]

    def warmup_bars(self, params: dict, timeframe_label: str) -> int:
        if timeframe_label == "M15":
            return int(params.get("ml_warmup_bars", 900))
        if timeframe_label == "M1":
            return 10
        return 50

    # A predikció állapotmentes (nincs mély állapotgép) → a jelzés-warmup
    # megegyezik az indikátor-warmuppal (a Strategy default pont ezt adja).

    def compute_display(self, md: MarketData) -> dict[str, Cell]:
        """Rekonstrukció adatból (amikor nincs élő motor-state): az utolsó ZÁRT
        M15 gyertya predikciója."""
        cells = dict(_MARKS_EMPTY)
        cells["ml_proba"] = Cell("—", "muted")
        df15 = md.bars.get("M15")
        pip = md.params.get("pip_size")
        if df15 is None or len(df15) < 3 or not pip:
            return cells
        bundle = load_bundle(md.symbol)
        cells["model"] = _stage_bool(bundle is not None)
        if bundle is None:
            return cells
        try:
            closed = df15.iloc[:-1]                 # a formálódó gyertya levágva
            feats = mlf.build_feature_frame(closed, float(pip))
            tail = feats.iloc[[-1]]
            p_long, p_short = _predict_frame(tail, bundle)
            thr_l, thr_s = _thresholds(bundle)
            hour = int(closed.index[-1].hour)
            sig = _evaluate(p_long[-1], p_short[-1], thr_l, thr_s, hour, md.params)
            cells["sig"] = _stage_signal(sig)
            cells["ml_proba"] = _proba_cell(p_long[-1], p_short[-1], thr_l, thr_s)
        except Exception as ex:
            log.debug("%s — ML display hiba: %s", md.symbol, ex)
        return cells

    # --- Élő jelzéslogika (ZÁRT gyertyán, állapottartó) --------------------

    def new_signal_state(self, symbol: str) -> MlAiState:
        return MlAiState(symbol)

    def on_bar_close(self, state: MlAiState, md: MarketData) -> tuple[MlAiState, str]:
        """Új ZÁRT M15 gyertyánál predikció → jel. A hívás M1-ütemű; ugyanarra a
        zárt M15 gyertyára csak EGYSZER értékelünk (és tüzelünk)."""
        df15 = md.bars.get("M15")
        pip = md.params.get("pip_size")
        if df15 is None or len(df15) < 3 or not pip:
            return state, "NONE"

        closed = df15.iloc[:-1]                     # az utolsó sor a formálódó
        t = closed.index[-1]
        if state.last_m15_time is not None and t == state.last_m15_time:
            return state, "NONE"                    # ezt a gyertyát már értékeltük
        state.last_m15_time = t

        bundle = load_bundle(md.symbol)
        state.model_ok = bundle is not None
        if bundle is None:
            state.p_long = state.p_short = float("nan")
            state.last_signal = "NONE"
            return state, "NONE"

        try:
            feats = mlf.build_feature_frame(closed, float(pip))
            p_long, p_short = _predict_frame(feats.iloc[[-1]], bundle)
        except Exception as ex:
            log.warning("%s — ML feature/predikció hiba: %s", md.symbol, ex)
            return state, "NONE"

        state.thr_long, state.thr_short = _thresholds(bundle)
        state.p_long, state.p_short = float(p_long[-1]), float(p_short[-1])
        hour = int(t.hour)
        state.sess_ok = _sess_ok(hour, md.params)
        signal = _evaluate(state.p_long, state.p_short, state.thr_long,
                           state.thr_short, hour, md.params)
        state.last_signal = signal
        if signal != "NONE":
            log.info("📊 %s → %s jelzés (ML) | P(long)=%.3f (küszöb %.2f) | "
                     "P(short)=%.3f (küszöb %.2f) | óra=%d",
                     md.symbol, signal, state.p_long, state.thr_long,
                     state.p_short, state.thr_short, hour)
        return state, signal

    def live_cells(self, state: MlAiState, md: MarketData) -> dict[str, Cell]:
        return {
            "model": _stage_bool(state.model_ok),
            "sig":   _stage_signal(state.last_signal),
            "ml_proba": _proba_cell(state.p_long, state.p_short,
                                    state.thr_long, state.thr_short),
        }

    # --- Optimalizálás (Fázis 2: az Opt gomb TANÍTÁST futtat) --------------

    def base_params(self, cfg: dict) -> dict:
        return {**cfg.get("indicators", {}), **cfg.get("sltp", {}),
                **cfg.get("position_mgmt", {})}

    def param_space(self, cfg: dict, base_params: dict, method: str,
                    max_trials: int) -> list[dict]:
        # Az ML-stratégiánál nincs paraméter-rács: az „optimalizálás" a modell
        # tanítása (fit). Egyetlen alap-kombináció, hogy a meglévő optimizer-
        # életciklus (teszt-backtest, minősítés) lefuthasson.
        return [dict(base_params)]

    def fit(self, symbol: str, df_m15, cfg: dict, pair_cfg: dict,
            test_start: str, progress_callback=None) -> dict:
        """Modell-tanítás — az optimizer fit-dispatch-e hívja (az Opt gomb az
        ml_ai-nál TANÍT). A test_start UTÁNI adat nem kerül tanításba; az
        out-of-sample mérést az optimizer közös run_pair-teszt végzi a frissen
        mentett modellel."""
        from strategy.ml_train import train_symbol
        return train_symbol(symbol, df_m15, cfg, pair_cfg, test_start,
                            progress_callback)

    # --- Azonosítás: MT5 magic ---------------------------------------------

    def magic(self, cfg: dict) -> int:
        return int(cfg.get("broker", {}).get("magic", 0)) + MAGIC_OFFSET

    # --- Backtest-motor hookok ---------------------------------------------

    def bt_indicators(self, df_hi, df_lo, params):
        """Feature-ök + VEKTOROS predikció a teljes M15 frame-re (a hookok szoros
        ciklusban futnak — soronkénti predict tiltó lassú volna). A p_long/p_short
        oszlop a SAJÁT sora záróadatából számolt predikció; a kauzális eltolást
        (előző zárt sor jele) a bt-állapotgép végzi. Igényli: params['symbol'] és
        params['pip_size'] (a motor injektálja a pair configból)."""
        pip = params.get("pip_size")
        symbol = params.get("symbol", "")
        if not pip:
            raise ValueError("ml_ai.bt_indicators: hiányzó pip_size a params-ból "
                             "(a hívónak a pair configból kell injektálnia)")
        feats = mlf.build_feature_frame(df_hi, float(pip))
        bundle = load_bundle(symbol)
        if bundle is None:
            log.warning("%s — nincs tanított ML modell (%s), a stratégia nem ad jelet",
                        symbol, model_path(symbol).name)
        p_long, p_short = _predict_frame(feats, bundle)
        thr_l, thr_s = _thresholds(bundle)
        feats["p_long"]  = p_long
        feats["p_short"] = p_short
        feats["thr_long"]  = thr_l
        feats["thr_short"] = thr_s
        return feats, df_lo

    def bt_warmup(self, params: dict, timeframe_label: str) -> int:
        if timeframe_label == "M15":
            return mlf.WARMUP_BARS
        return 2

    def bt_new_state(self, symbol: str) -> MlAiBtState:
        return MlAiBtState(symbol)

    def bt_on_high_close(self, state: MlAiBtState, hi_row, params):
        """A motor M1-enként hívja az AKTUÁLIS (formálódó) M15 sorral. Új sor
        érkezésekor az ELŐZŐ (= most zárt) sor predikciójából képezzük a jelet
        (kauzális), majd az új sor értékeit eltesszük a következő váltáshoz."""
        key = hi_row.name
        if key == state.last_key:
            return state
        # Új M15 sor → az előző sor (utolsó zárt gyertya) jele élesedik
        if state.prev_valid:
            state.pending = _evaluate(state.prev_p_long, state.prev_p_short,
                                      float(hi_row.get("thr_long", 1.01)),
                                      float(hi_row.get("thr_short", 1.01)),
                                      state.prev_hour, params)
        state.last_key = key
        pl, ps = hi_row.get("p_long"), hi_row.get("p_short")
        state.prev_p_long  = float(pl) if pl is not None and not pd.isna(pl) else 0.0
        state.prev_p_short = float(ps) if ps is not None and not pd.isna(ps) else 0.0
        state.prev_hour = int(key.hour) if hasattr(key, "hour") else -1
        state.prev_valid = True
        return state

    def bt_on_low_close(self, state: MlAiBtState, prev_lo_row, lo_row, params) -> str:
        # Az élesített jel az M15 váltás utáni ELSŐ M1 záráskor tüzel — egyszer.
        sig = state.pending
        state.pending = "NONE"
        return sig

    def sl_tp_pips(self, hi_row, params, pip_size):
        """SL/TP méret: ATR-alapú (dynamic_sltp, alap) vagy fix pip. Az ATR a
        hi_row atr14 oszlopából (a feature-motor számolja)."""
        if params.get("dynamic_sltp", True):
            atr_v = hi_row.get("atr14", 0)
            if not atr_v or pd.isna(atr_v) or atr_v <= 0:
                return None
            sl = float(atr_v) / float(pip_size) * float(params.get("sl_atr_mult", 1.5))
        else:
            sl = float(params.get("sl_pips", 0) or 0)
            if sl <= 0:
                return None
        tp = sl * float(params.get("tp_rr_ratio", 2.0))
        return sl, tp
