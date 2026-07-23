"""Session Chart backend — proxy propio a Yahoo Finance.
Deploy en Render: build = pip install -r requirements.txt
                  start = uvicorn main:app --host 0.0.0.0 --port $PORT
"""
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
import httpx

app = FastAPI(title="Session Chart API")

# CORS abierto: permite que la versión standalone del HTML también use este
# backend como fuente de datos si algún día la abres como archivo local.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
ALLOWED_INTERVALS = {"1m", "2m", "5m", "15m", "30m", "60m", "90m", "1d"}


@app.get("/api/chart")
async def chart(symbol: str, period1: int, period2: int, interval: str = "5m"):
    if interval not in ALLOWED_INTERVALS:
        raise HTTPException(400, f"intervalo no soportado: {interval}")
    if not symbol or len(symbol) > 12 or period2 <= period1:
        raise HTTPException(400, "parámetros inválidos")
    params = {
        "period1": period1,
        "period2": period2,
        "interval": interval,
        "includePrePost": "false",
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(YAHOO.format(sym=symbol.upper()), params=params, headers=UA)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"no se pudo contactar a Yahoo: {e.__class__.__name__}")
    if r.status_code != 200:
        raise HTTPException(502, f"Yahoo respondió HTTP {r.status_code}")
    return JSONResponse(r.json())


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse("index.html")
