"""
escalera_signals.py
===================
Detector de rachas (streak) + filtro de riesgo por earnings para la
metodologia "escalera ascendente".

Filosofia del modulo
--------------------
NO es un buscador de oportunidades. Es un verificador de disciplina.
Se le pasa una watchlist corta (los tickers que ya conoces) y responde
tres preguntas por cada uno:

  1. Hay una racha de acumulacion confirmada?  -> compute_streak()
  2. Hay riesgo de gap por earnings?           -> get_earnings_risk()
  3. Ya esta extendido (parabolico)?           -> extension en ATR

Reglas de la escalera que este modulo SI puede verificar:
  - Higher lows confirmados      -> low > low anterior, dia a dia
  - RVOL > 1.5x                  -> volumen / media movil de volumen
  - No perseguir parabolico      -> extension desde SMA en unidades de ATR

Regla que NO puede verificar (es de gestion de posicion, no de precio):
  - No promediar a la baja. Eso depende de tu ejecucion, no del screener.

Dependencias: pandas, numpy, yfinance
"""

from __future__ import annotations

import math
from dataclasses import dataclass, asdict, field
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

MARKET_TZ = ZoneInfo("America/New_York")

# ---------------------------------------------------------------------------
# Parametros por defecto
# ---------------------------------------------------------------------------

DEFAULTS = {
    "vol_ma_period": 20,      # media movil de volumen para el RVOL
    "atr_period": 14,         # periodo del ATR
    "sma_period": 20,         # SMA de referencia para medir extension
    "min_rvol": 1.5,          # umbral de la escalera
    "max_extension_atr": 3.0, # por encima de esto se considera parabolico
    "min_streak": 2,          # dias minimos de racha para considerarlo valido
}


# ---------------------------------------------------------------------------
# 1. DETECTOR DE RACHA
# ---------------------------------------------------------------------------

@dataclass
class StreakResult:
    """Resultado del analisis de racha para un ticker."""

    symbol: str
    ok: bool = False
    reason: str = ""

    streak_days: int = 0
    last_close: float = float("nan")
    last_rvol: float = float("nan")
    avg_rvol_streak: float = float("nan")
    min_rvol_streak: float = float("nan")
    gain_pct_streak: float = float("nan")

    extension_atr: float = float("nan")
    atr: float = float("nan")
    sma: float = float("nan")
    parabolic: bool = False

    # Checklist de la escalera
    higher_lows: bool = False
    rvol_ok: bool = False
    not_parabolic: bool = False
    escalera_ok: bool = False

    days: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return _clean_nans(asdict(self))


def _clean_nans(obj: Any) -> Any:
    """Convierte NaN/inf a None para que FastAPI pueda serializar a JSON."""
    if isinstance(obj, dict):
        return {k: _clean_nans(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean_nans(v) for v in obj]
    if isinstance(obj, (float, np.floating)):
        f = float(obj)
        return None if (math.isnan(f) or math.isinf(f)) else round(f, 4)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    return obj


def _true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["Close"].shift(1)
    return pd.concat(
        [
            df["High"] - df["Low"],
            (df["High"] - prev_close).abs(),
            (df["Low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)


def compute_streak(
    df: pd.DataFrame,
    symbol: str = "",
    vol_ma_period: int = DEFAULTS["vol_ma_period"],
    atr_period: int = DEFAULTS["atr_period"],
    sma_period: int = DEFAULTS["sma_period"],
    min_rvol: float = DEFAULTS["min_rvol"],
    max_extension_atr: float = DEFAULTS["max_extension_atr"],
    min_streak: int = DEFAULTS["min_streak"],
) -> StreakResult:
    """
    Cuenta dias consecutivos que cumplen las tres condiciones a la vez:

        close > close anterior      (dia alcista)
        low   > low anterior        (higher low confirmado)
        volume > media movil vol    (volumen por encima del promedio)

    La racha se cuenta hacia atras desde la ultima vela disponible.
    Si el ultimo dia no califica, la racha es 0 aunque haya habido una
    racha larga que se rompio ayer.

    df: DataFrame con columnas Open/High/Low/Close/Volume, indice de fechas
        en orden ascendente (el mas viejo primero).
    """
    res = StreakResult(symbol=symbol)

    required = {"High", "Low", "Close", "Volume"}
    if df is None or not required.issubset(df.columns):
        res.reason = "faltan columnas OHLCV"
        return res

    d = df.dropna(subset=["High", "Low", "Close", "Volume"]).copy()
    d = d.sort_index()

    warmup = max(vol_ma_period, atr_period, sma_period) + 2
    if len(d) < warmup:
        res.reason = f"historial insuficiente ({len(d)} velas, se necesitan {warmup})"
        return res

    d["vol_ma"] = d["Volume"].rolling(vol_ma_period).mean()
    d["rvol"] = d["Volume"] / d["vol_ma"]
    d["atr"] = _true_range(d).rolling(atr_period).mean()
    d["sma"] = d["Close"].rolling(sma_period).mean()

    up_day = d["Close"] > d["Close"].shift(1)
    higher_low = d["Low"] > d["Low"].shift(1)
    vol_above = d["Volume"] > d["vol_ma"]
    qualifies = (up_day & higher_low & vol_above).fillna(False)

    # Contar hacia atras desde el final
    streak = 0
    for val in reversed(qualifies.tolist()):
        if bool(val):
            streak += 1
        else:
            break

    last = d.iloc[-1]
    res.last_close = float(last["Close"])
    res.last_rvol = float(last["rvol"])
    res.atr = float(last["atr"])
    res.sma = float(last["sma"])
    res.streak_days = streak

    # Extension desde la SMA en unidades de ATR (proxy de "parabolico")
    if res.atr and res.atr > 0:
        res.extension_atr = (res.last_close - res.sma) / res.atr
    res.parabolic = bool(
        not math.isnan(res.extension_atr) and res.extension_atr > max_extension_atr
    )

    if streak == 0:
        res.ok = True
        res.reason = "sin racha activa — el último día no califica"
        res.not_parabolic = not res.parabolic
        return res

    window = d.iloc[-streak:]
    res.avg_rvol_streak = float(window["rvol"].mean())
    res.min_rvol_streak = float(window["rvol"].min())

    base_idx = len(d) - streak - 1
    if base_idx >= 0:
        base_close = float(d["Close"].iloc[base_idx])
        if base_close > 0:
            res.gain_pct_streak = (res.last_close / base_close - 1.0) * 100.0

    res.days = [
        {
            "date": str(idx.date() if hasattr(idx, "date") else idx),
            "close": float(row["Close"]),
            "low": float(row["Low"]),
            "volume": int(row["Volume"]),
            "rvol": float(row["rvol"]),
        }
        for idx, row in window.iterrows()
    ]

    # Checklist de la escalera
    res.higher_lows = streak >= min_streak
    res.rvol_ok = bool(res.min_rvol_streak >= min_rvol)
    res.not_parabolic = not res.parabolic
    res.escalera_ok = bool(res.higher_lows and res.rvol_ok and res.not_parabolic)

    res.ok = True
    res.reason = "ok"
    return res


# ---------------------------------------------------------------------------
# 2. FILTRO DE EARNINGS
# ---------------------------------------------------------------------------

@dataclass
class EarningsRisk:
    """Riesgo de gap por earnings para un ticker."""

    symbol: str
    has_event: bool = False
    event_date: Optional[str] = None
    timing: str = "unknown"          # "bmo" | "amc" | "unknown"
    days_away: Optional[int] = None
    blocks_overnight: bool = False   # True -> no cargar overnight hoy
    blocks_intraday: bool = False    # True -> reporta hoy, regimen distinto
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return _clean_nans(asdict(self))


def _classify_timing(ts: pd.Timestamp) -> str:
    """
    Infiere BMO/AMC por la hora del timestamp de yfinance (hora de mercado).
    yfinance suele dar 07:00-09:00 ET para BMO y 16:00-20:00 ET para AMC.
    """
    try:
        if ts.tzinfo is not None:
            local = ts.tz_convert(MARKET_TZ)
        else:
            local = ts.tz_localize(MARKET_TZ)
    except Exception:
        return "unknown"

    hour = local.hour
    if hour == 0 and local.minute == 0:
        return "unknown"   # solo fecha, sin hora real
    if hour < 9 or (hour == 9 and local.minute < 30):
        return "bmo"
    if hour >= 16:
        return "amc"
    return "unknown"


def get_earnings_risk(
    symbol: str,
    today: Optional[date] = None,
    lookahead_days: int = 2,
    ticker_obj: Any = None,
) -> EarningsRisk:
    """
    Determina si mantener el ticker esta noche te expone a un gap por earnings.

    blocks_overnight = True cuando:
        - reporta HOY despues del cierre (AMC)
        - reporta MANANA antes de la apertura (BMO)
        - reporta MANANA con horario desconocido  (se asume riesgoso)

    Nota: los datos de earnings de yfinance son estimados y a veces vienen
    vacios. Si no se puede determinar, has_event queda en False y `note`
    lo explica: ausencia de dato NO es lo mismo que ausencia de earnings.
    """
    risk = EarningsRisk(symbol=symbol)
    today = today or datetime.now(MARKET_TZ).date()

    if ticker_obj is not None:
        tk = ticker_obj
    else:
        import yfinance as yf  # import diferido: permite inyectar un mock
        tk = yf.Ticker(symbol)
    stamps: list[pd.Timestamp] = []

    # Fuente 1: earnings_dates (DataFrame indexado por datetime)
    try:
        ed = tk.earnings_dates
        if ed is not None and len(ed) > 0:
            stamps.extend(pd.Timestamp(i) for i in ed.index)
    except Exception:
        pass

    # Fuente 2: calendar (dict o DataFrame segun version de yfinance)
    try:
        cal = tk.calendar
        raw: Iterable[Any] = []
        if isinstance(cal, dict):
            raw = cal.get("Earnings Date") or []
            if not isinstance(raw, (list, tuple)):
                raw = [raw]
        elif isinstance(cal, pd.DataFrame) and "Earnings Date" in cal.index:
            raw = list(cal.loc["Earnings Date"].values)
        for v in raw:
            if v is not None:
                stamps.append(pd.Timestamp(v))
    except Exception:
        pass

    if not stamps:
        risk.note = "yfinance no devolvió fecha de earnings — dato ausente, no confirmado sin evento"
        return risk

    # Quedarse con el evento futuro mas cercano (o el de hoy)
    horizon = today + timedelta(days=lookahead_days)
    candidates = []
    for ts in stamps:
        try:
            d = ts.date()
        except Exception:
            continue
        if today <= d <= horizon:
            candidates.append(ts)

    if not candidates:
        risk.note = f"sin earnings en los próximos {lookahead_days} días"
        return risk

    ts = min(candidates, key=lambda t: t.date())
    ev_date = ts.date()
    timing = _classify_timing(ts)
    days_away = (ev_date - today).days

    risk.has_event = True
    risk.event_date = str(ev_date)
    risk.timing = timing
    risk.days_away = days_away

    if days_away == 0:
        risk.blocks_intraday = True
        if timing == "amc":
            risk.blocks_overnight = True
            risk.note = "reporta HOY después del cierre — no cargar overnight"
        elif timing == "bmo":
            risk.note = "reportó HOY antes de la apertura — régimen post-earnings"
        else:
            risk.blocks_overnight = True
            risk.note = "reporta HOY, horario desconocido — tratar como riesgoso"
    elif days_away == 1:
        risk.blocks_overnight = True
        risk.note = "reporta MAÑANA — no cargar overnight esta noche"
    else:
        risk.note = f"earnings en {days_away} días"

    return risk


# ---------------------------------------------------------------------------
# 3. SCAN DE WATCHLIST
# ---------------------------------------------------------------------------

def fetch_history(
    symbols: list[str],
    period: str = "4mo",
    interval: str = "1d",
) -> dict[str, pd.DataFrame]:
    """
    Descarga OHLCV diario en lote. Devuelve {symbol: DataFrame}.
    Usa auto_adjust=False para conservar volumen y precios sin ajustar,
    que es lo que corresponde para RVOL intradiario.
    """
    import yfinance as yf

    symbols = [s.strip().upper() for s in symbols if s and s.strip()]
    if not symbols:
        return {}

    raw = yf.download(
        tickers=symbols,
        period=period,
        interval=interval,
        group_by="ticker",
        auto_adjust=False,
        progress=False,
        threads=True,
    )

    out: dict[str, pd.DataFrame] = {}
    if isinstance(raw.columns, pd.MultiIndex):
        for sym in symbols:
            if sym in raw.columns.get_level_values(0):
                out[sym] = raw[sym].dropna(how="all")
    else:
        # Un solo ticker: yfinance devuelve columnas planas
        out[symbols[0]] = raw.dropna(how="all")
    return out


def scan_watchlist(
    symbols: list[str],
    check_earnings: bool = True,
    period: str = "4mo",
    **params: Any,
) -> list[dict[str, Any]]:
    """
    Escanea una watchlist y devuelve una lista ordenada de resultados,
    lista para servir desde FastAPI.

    Orden: primero los que cumplen la escalera completa, luego por
    dias de racha, luego por RVOL promedio.
    """
    hist = fetch_history(symbols, period=period)
    rows: list[dict[str, Any]] = []

    for sym in [s.strip().upper() for s in symbols if s and s.strip()]:
        df = hist.get(sym)
        streak = compute_streak(df, symbol=sym, **params) if df is not None else StreakResult(
            symbol=sym, reason="sin datos"
        )
        row = streak.to_dict()

        if check_earnings:
            try:
                er = get_earnings_risk(sym)
            except Exception as exc:  # nunca tumbar el scan por un ticker
                er = EarningsRisk(symbol=sym, note=f"error consultando earnings: {exc}")
            row["earnings"] = er.to_dict()
            # La escalera se anula si hay riesgo de gap overnight
            if er.blocks_overnight and row.get("escalera_ok"):
                row["escalera_ok"] = False
                row["blocked_by"] = "earnings"
        rows.append(row)

    rows.sort(
        key=lambda r: (
            not bool(r.get("escalera_ok")),
            -(r.get("streak_days") or 0),
            -(r.get("avg_rvol_streak") or 0),
        )
    )
    return rows


# ---------------------------------------------------------------------------
# 4. ENDPOINT FASTAPI (para pegar en tu app existente)
# ---------------------------------------------------------------------------

FASTAPI_SNIPPET = '''
from fastapi import APIRouter, Query
from escalera_signals import scan_watchlist

router = APIRouter()

WATCHLIST = ["MU", "SNDK", "MRVL", "ARM", "BE", "AXTI"]

@router.get("/escalera/scan")
def escalera_scan(
    symbols: str = Query(default=",".join(WATCHLIST)),
    earnings: bool = Query(default=True),
    min_rvol: float = Query(default=1.5),
    max_extension_atr: float = Query(default=3.0),
):
    syms = [s for s in symbols.split(",") if s.strip()]
    return {
        "count": len(syms),
        "results": scan_watchlist(
            syms,
            check_earnings=earnings,
            min_rvol=min_rvol,
            max_extension_atr=max_extension_atr,
        ),
    }
'''


if __name__ == "__main__":
    import json
    import sys

    syms = sys.argv[1:] or ["MU", "SNDK", "MRVL", "ARM", "BE", "AXTI"]
    print(json.dumps(scan_watchlist(syms), indent=2, default=str))
