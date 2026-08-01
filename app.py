"""
app.py
======
API de la escalera ascendente. Se despliega en Render igual que tu
screener actual (FastAPI + yfinance).

Endpoints
---------
GET /                      salud y version
GET /scan                  checklist completo de la watchlist
GET /streak                solo rachas, sin reglas
GET /pair/{underlying}     par 2x largo/corto y avisos de asimetria
GET /size                  dimensionamiento de una entrada concreta
GET /signals/performance   win rate real de las senales vs base rate
GET /universe              catalogo de ETFs apalancados verificados

Ejecutar local:  uvicorn app:app --reload
Render:          uvicorn app:app --host 0.0.0.0 --port $PORT
"""

from __future__ import annotations

import os
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from storage import store
from escalera_checklist import (
    DEFAULT_WATCHLIST,
    RISK_DEFAULTS,
    evaluate_signals,
    scan,
    size_position,
)
from escalera_signals import DEFAULTS, fetch_history, scan_watchlist
from leveraged_map import (
    CATALOG,
    VERIFIED_AS_OF,
    check_pair,
    get_etf,
    pairs_report,
    underlyings,
    universe,
)

app = FastAPI(
    title="Escalera Ascendente",
    description="Verificador de disciplina para day trading de 2x de accion unica",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


def _split(symbols: Optional[str]) -> Optional[list[str]]:
    if not symbols:
        return None
    return [s.strip().upper() for s in symbols.split(",") if s.strip()]


@app.get("/")
def ui():
    """Interfaz."""
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "index.html"))


@app.get("/api")
def root():
    return {
        "service": "escalera-ascendente",
        "version": "1.0.0",
        "catalog_verified_as_of": VERIFIED_AS_OF,
        "catalog_size": len(CATALOG),
        "default_watchlist": DEFAULT_WATCHLIST,
        "storage": store.status(),
        "endpoints": [
            "/scan", "/streak", "/pair/{underlying}", "/size",
            "/signals/performance", "/signals/status", "/universe",
        ],
    }


@app.get("/signals/status")
def signals_status():
    """Estado del almacenamiento del log y cuantas senales hay guardadas."""
    st = store.status()
    try:
        st["count"] = store.count()
    except Exception:
        st["count"] = None
    return st


@app.get("/scan")
def scan_endpoint(
    symbols: Optional[str] = Query(None, description="CSV; por defecto la watchlist"),
    equity: float = Query(0, ge=0, description="Equity actual para dimensionar"),
    earnings: bool = Query(True, description="Chequear riesgo de earnings"),
    period: str = Query("4mo"),
    log: bool = Query(True, description="Registrar senales para medir win rate"),
    min_rvol: float = Query(DEFAULTS["min_rvol"], gt=0),
    max_extension_atr: float = Query(DEFAULTS["max_extension_atr"], gt=0),
    min_streak: int = Query(DEFAULTS["min_streak"], ge=1),
    risk_pct: float = Query(RISK_DEFAULTS["risk_pct"], gt=0, le=0.2),
    max_position_pct: float = Query(RISK_DEFAULTS["max_position_pct"], gt=0, le=1.0),
    min_adv_usd: float = Query(RISK_DEFAULTS["min_adv_usd"], ge=0),
):
    """Checklist completo. Ordena GO -> WAIT -> BLOCK."""
    return scan(
        symbols=_split(symbols),
        equity=equity,
        check_earnings=earnings,
        period=period,
        log=log,
        risk={
            "risk_pct": risk_pct,
            "max_position_pct": max_position_pct,
            "min_adv_usd": min_adv_usd,
        },
        min_rvol=min_rvol,
        max_extension_atr=max_extension_atr,
        min_streak=min_streak,
    )


@app.get("/streak")
def streak_endpoint(
    symbols: Optional[str] = Query(None),
    earnings: bool = Query(True),
    period: str = Query("4mo"),
    min_rvol: float = Query(DEFAULTS["min_rvol"], gt=0),
    max_extension_atr: float = Query(DEFAULTS["max_extension_atr"], gt=0),
):
    """Solo rachas y earnings, sin el motor de reglas."""
    syms = _split(symbols) or DEFAULT_WATCHLIST
    return {
        "count": len(syms),
        "results": scan_watchlist(
            syms,
            check_earnings=earnings,
            period=period,
            min_rvol=min_rvol,
            max_extension_atr=max_extension_atr,
        ),
    }


@app.get("/pair/{underlying}")
def pair_endpoint(underlying: str):
    """Par 2x largo/corto del subyacente, con avisos de asimetria."""
    info = check_pair(underlying)
    if not info["longs"] and not info["shorts"]:
        raise HTTPException(
            status_code=404,
            detail=f"{underlying.upper()} no esta en el catalogo verificado. "
                   f"Subyacentes disponibles: {', '.join(underlyings())}",
        )
    return info


@app.get("/pairs")
def pairs_endpoint():
    """Mapa completo: que subyacentes admiten par largo/corto ejecutable."""
    report = pairs_report()
    return {
        "verified_as_of": VERIFIED_AS_OF,
        "tradeable": sum(1 for r in report if r["tradeable_pair"]),
        "symmetric": sum(1 for r in report if r["symmetric"]),
        "total": len(report),
        "results": report,
    }


@app.get("/size")
def size_endpoint(
    equity: float = Query(..., gt=0),
    entry: float = Query(..., gt=0),
    atr: float = Query(..., gt=0),
    symbol: Optional[str] = Query(None, description="Para aplicar el factor de apalancamiento"),
    adv_usd: Optional[float] = Query(None, gt=0),
    risk_pct: float = Query(RISK_DEFAULTS["risk_pct"], gt=0, le=0.2),
    atr_stop_mult: float = Query(RISK_DEFAULTS["atr_stop_mult"], gt=0),
    max_position_pct: float = Query(RISK_DEFAULTS["max_position_pct"], gt=0, le=1.0),
    max_pct_of_adv: float = Query(RISK_DEFAULTS["max_pct_of_adv"], gt=0, le=1.0),
):
    """Dimensionamiento por distancia al stop con topes de concentracion y liquidez."""
    factor = 1.0
    if symbol:
        e = get_etf(symbol)
        if e:
            factor = abs(e.factor)
    result = size_position(
        equity=equity, entry=entry, atr=atr, leverage_factor=factor,
        risk_pct=risk_pct, atr_stop_mult=atr_stop_mult,
        max_position_pct=max_position_pct, adv_usd=adv_usd,
        max_pct_of_adv=max_pct_of_adv,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("reason", "parametros invalidos"))
    return result


@app.get("/signals/performance")
def performance_endpoint(
    horizon_days: int = Query(1, ge=1, le=20),
    period: str = Query("6mo"),
):
    """
    Win rate real de las senales registradas contra la base rate del propio
    ticker. Si edge_vs_base_pct no es positivo, el screener no aporta.
    """
    return evaluate_signals(horizon_days=horizon_days, period=period)


@app.get("/universe")
def universe_endpoint(kind: str = Query("all", pattern="^(all|long|short)$")):
    return {
        "verified_as_of": VERIFIED_AS_OF,
        "kind": kind,
        "tickers": universe(kind),
        "underlyings": underlyings(),
    }
