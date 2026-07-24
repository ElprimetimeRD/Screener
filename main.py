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


# ================= descubrimiento de volumen brusco en el mercado =================
import statistics
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
SESSION_OPEN_MIN = 9 * 60 + 30
SESSION_CLOSE_MIN = 16 * 60

# Universo de respaldo si los screeners de Yahoo no responden
FALLBACK_UNIVERSE = [
    "NVDA","AMD","MU","SNDK","MRVL","ARM","INTC","TSM","QCOM","AVGO","SMCI",
    "AAPL","MSFT","AMZN","META","GOOGL","TSLA","PLTR","COIN","MSTR","HOOD",
    "SOFI","RIVN","LCID","NIO","F","BAC","T","PFE","XOM","CVX","OXY","AAL",
    "CCL","UBER","ABNB","SHOP","PYPL","SNAP","BABA","PDD",
]


def detect_spikes(bars, k=3.0, base_win=20, min_vol=100000):
    """bars: lista de {t, open, close, volume} en orden temporal.
    Mismo algoritmo que el frontend: vol >= k × mediana de los base_win
    minutos previos; minutos consecutivos se agrupan en un evento."""
    n = len(bars)
    flags = [False] * n
    mult = [0.0] * n
    warmup = min(base_win, 10)
    for i in range(n):
        prior = [b["volume"] for b in bars[max(0, i - base_win):i] if b["volume"] > 0]
        if len(prior) < warmup:
            continue
        base = statistics.median(prior)
        if base <= 0:
            continue
        mult[i] = bars[i]["volume"] / base
        if bars[i]["volume"] >= min_vol and mult[i] >= k:
            flags[i] = True
    events = []
    i = 0
    while i < n:
        if not flags[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and flags[j + 1]:
            j += 1
        vol = sum(b["volume"] for b in bars[i:j + 1])
        peak = max(mult[i:j + 1])
        ref = bars[i - 1]["close"] if i > 0 else bars[i]["open"]
        c = bars[j]["close"]
        chg = (c - ref) / ref * 100 if ref else 0.0
        tipo = "absorción" if abs(chg) < 0.1 else ("comprador" if chg > 0 else "vendedor")
        events.append({
            "start": bars[i]["t"], "end": bars[j]["t"], "minutes": j - i + 1,
            "vol": int(vol), "peak": round(peak, 2), "chg": round(chg, 2),
            "tipo": tipo, "price": round(float(c), 2),
        })
        i = j + 1
    return events


def _clean_symbol(s):
    return bool(s) and s.isalpha() and len(s) <= 5


def get_universe(cap=45):
    """Los más activos + mayores gainers/losers del día según Yahoo."""
    syms, note = [], None
    for scr in ("most_actives", "day_gainers", "day_losers"):
        try:
            res = yf.screen(scr, count=25)
            quotes = res.get("quotes", []) if isinstance(res, dict) else []
            for q in quotes:
                s = (q.get("symbol") or "").upper()
                if _clean_symbol(s) and s not in syms:
                    syms.append(s)
        except Exception:
            continue
    if not syms:
        syms = FALLBACK_UNIVERSE[:]
        note = "screeners de Yahoo no disponibles; usando universo fijo de respaldo"
    return syms[:cap], note


def bars_from_frame(sub):
    """Convierte el sub-DataFrame de un símbolo a la lista de barras del detector."""
    if sub is None or sub.empty:
        return []
    sub = sub.dropna(subset=["Open", "Close"])
    bars = []
    for ix, r in sub.iterrows():
        v = r.get("Volume")
        bars.append({
            "t": int(ix.timestamp()),
            "open": float(r["Open"]),
            "close": float(r["Close"]),
            "volume": 0 if pd.isna(v) else int(v),
        })
    return bars


@app.get("/api/discover")
def discover(k: float = 3.0, base: int = 20, min_vol: int = 100000):
    if not (1.0 <= k <= 50) or not (5 <= base <= 120):
        raise HTTPException(400, "parámetros fuera de rango")
    syms, note = get_universe()
    try:
        df = yf.download(
            tickers=syms, period="1d", interval="1m",
            prepost=False, auto_adjust=False, progress=False,
            threads=True, group_by="ticker",
        )
    except Exception as e:
        raise HTTPException(502, f"descarga falló: {e.__class__.__name__}")

    now_et = datetime.now(tz=ET)
    mins = now_et.hour * 60 + now_et.minute
    cutoff = None
    if SESSION_OPEN_MIN <= mins <= SESSION_CLOSE_MIN and now_et.weekday() < 5:
        cutoff = int(datetime.now(timezone.utc).timestamp()) - 45 * 60

    events, scanned = [], 0
    for s in syms:
        try:
            if isinstance(df.columns, pd.MultiIndex):
                if s not in df.columns.get_level_values(0):
                    continue
                sub = df[s]
            else:
                sub = df
            bars = bars_from_frame(sub)
            if not bars:
                continue
            scanned += 1
            for e in detect_spikes(bars, k, base, min_vol):
                if cutoff and e["end"] < cutoff:
                    continue
                e["sym"] = s
                events.append(e)
        except Exception:
            continue
    events.sort(key=lambda e: (-e["start"], -e["peak"]))
    return {
        "universe": len(syms), "scanned": scanned,
        "events": events[:80], "note": note,
        "recent_only": cutoff is not None,
    }
