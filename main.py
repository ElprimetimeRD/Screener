"""Session Chart backend v2 — usa yfinance (maneja cookies/crumb de Yahoo,
necesario porque Yahoo bloquea llamadas directas desde IPs de datacenter).
Render: build = pip install -r requirements.txt
        start = uvicorn main:app --host 0.0.0.0 --port $PORT
"""
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
import pandas as pd
import yfinance as yf

app = FastAPI(title="Session Chart API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

ALLOWED_INTERVALS = {"1m", "2m", "5m", "15m", "30m", "60m", "90m", "1d"}


def df_to_chart_json(df: pd.DataFrame) -> dict:
    """Convierte el DataFrame de yfinance al shape chart-API que espera el frontend."""
    if df is None or df.empty:
        return {"chart": {"result": [], "error": None}}
    # yfinance puede devolver columnas MultiIndex (Price, Ticker); aplanar
    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        df.columns = df.columns.get_level_values(0)
    ts = [int(idx.timestamp()) for idx in df.index]

    def col(name):
        if name not in df.columns:
            return [None] * len(df)
        return [None if pd.isna(v) else float(v) for v in df[name]]

    vol = [None if pd.isna(v) else int(v) for v in df["Volume"]] if "Volume" in df.columns else [None] * len(df)
    quote = {
        "open": col("Open"),
        "high": col("High"),
        "low": col("Low"),
        "close": col("Close"),
        "volume": vol,
    }
    return {"chart": {"result": [{"timestamp": ts, "indicators": {"quote": [quote]}, "meta": {}}], "error": None}}


@app.get("/api/chart")
def chart(symbol: str, period1: int, period2: int, interval: str = "5m"):
    if interval not in ALLOWED_INTERVALS:
        raise HTTPException(400, f"intervalo no soportado: {interval}")
    if not symbol or len(symbol) > 12 or period2 <= period1:
        raise HTTPException(400, "parámetros inválidos")
    try:
        df = yf.download(
            tickers=symbol.upper(),
            start=datetime.fromtimestamp(period1, tz=timezone.utc),
            end=datetime.fromtimestamp(period2, tz=timezone.utc),
            interval=interval,
            prepost=False,
            auto_adjust=False,
            progress=False,
            threads=False,
        )
    except Exception as e:
        raise HTTPException(502, f"yfinance falló: {e.__class__.__name__}: {e}")
    return df_to_chart_json(df)


@app.get("/api/diag")
def diag():
    """Diagnóstico rápido: intenta bajar 1 día de MU y reporta el resultado."""
    try:
        df = yf.download("MU", period="5d", interval="1d", progress=False, threads=False)
        rows = 0 if df is None else len(df)
        return {"ok": rows > 0, "rows": rows}
    except Exception as e:
        return {"ok": False, "error": f"{e.__class__.__name__}: {e}"}


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse("index.html")
