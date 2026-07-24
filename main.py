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


@app.get("/api/radar")
def radar(rvol_min: float = 2.5, move_min: float = 0.0, watch: str = ""):
    """Radar de apertura: caza movers ANTES de que corran.
    Combina tres huellas tempranas: volumen relativo (contra la curva intradía real,
    no lineal), aceleración del último tramo, y movimiento vs el cierre anterior.
    Fuera de sesión analiza la última jornada disponible en vez de quedar vacío."""
    if not (1.0 <= rvol_min <= 30) or not (0 <= move_min <= 100):
        raise HTTPException(400, "parámetros fuera de rango")
    pinned = [w for w in watch.replace(";", ",").replace(" ", ",").split(",") if w][:8]
    syms, note = get_universe(pinned=pinned)
    stats = daily_stats(syms)
    try:
        df = yf.download(tickers=syms, period="1d", interval="1m",
                         prepost=False, auto_adjust=False, progress=False,
                         threads=True, group_by="ticker")
    except Exception as e:
        raise HTTPException(502, f"descarga falló: {e.__class__.__name__}")

    now_et = datetime.now(tz=ET)
    mins_now = now_et.hour * 60 + now_et.minute
    live = (SESSION_OPEN_MIN <= mins_now <= SESSION_CLOSE_MIN) and now_et.weekday() < 5

    cands = []
    session_date = None
    for s in syms:
        try:
            sub = df[s] if isinstance(df.columns, pd.MultiIndex) else df
            bars = bars_from_frame(sub)
            if len(bars) < 3:
                continue
            # agrupar por fecha ET y quedarse con la sesión MÁS RECIENTE disponible
            by_date = {}
            for b in bars:
                et = datetime.fromtimestamp(b["t"], tz=ET)
                m = et.hour * 60 + et.minute
                if SESSION_OPEN_MIN <= m <= SESSION_CLOSE_MIN:
                    by_date.setdefault(et.date(), []).append(b)
            if not by_date:
                continue
            d = max(by_date)
            sess = by_date[d]
            if len(sess) < 3:
                continue
            if session_date is None or d > session_date:
                session_date = d

            st = stats.get(s) or {}
            adv = st.get("avg_vol")
            prev_close = st.get("prev_close")
            day_open = sess[0]["open"]
            last = sess[-1]["close"]
            elapsed = len(sess)

            # RVOL contra la curva intradía real (no lineal)
            cum = sum(b["volume"] for b in sess)
            frac = expected_vol_frac(elapsed)
            rvol = cum / (adv * frac) if (adv and frac > 0) else None

            # aceleración: últimos 5 min vs los 5 previos
            accel = None
            if elapsed >= 10:
                recent = sum(b["volume"] for b in sess[-5:])
                prior = sum(b["volume"] for b in sess[-10:-5])
                accel = round(recent / prior, 1) if prior > 0 else None

            gap = round((day_open - prev_close) / prev_close * 100, 2) if prev_close else None
            chg_total = round((last - prev_close) / prev_close * 100, 2) if prev_close else None
            chg_open = round((last - day_open) / day_open * 100, 2) if day_open else None
            move = abs(chg_total) if chg_total is not None else abs(chg_open or 0)

            is_pinned = s in [p.upper() for p in pinned]
            if not is_pinned:
                if rvol is None or rvol < rvol_min:
                    continue
                if move < move_min:
                    continue

            # urgencia: volumen anómalo + acelerando + con movimiento real de precio
            rv = rvol or 0
            if rv >= 5 and (accel or 0) >= 1.5 and move >= 3:
                grade = "caliente"
            elif rv >= 3 and ((accel or 0) >= 1.5 or move >= 2):
                grade = "activo"
            else:
                grade = "vigilar"

            cands.append({
                "sym": s, "price": round(float(last), 2),
                "rvol": round(rvol, 1), "accel": accel,
                "gap": gap, "chg_total": chg_total, "chg_open": chg_open,
                "elapsed": elapsed, "cum_vol": int(cum),
                "dvol": round(cum * last), "grade": grade,
                "pinned": is_pinned,
            })
        except Exception:
            continue

    order = {"caliente": 0, "activo": 1, "vigilar": 2}
    cands.sort(key=lambda c: (not c.get("pinned"), order[c["grade"]], -(c["rvol"] or 0)))
    return {"universe": len(syms), "candidates": cands[:40],
            "mins_into_session": max(0, mins_now - SESSION_OPEN_MIN) if live else 390,
            "live": live, "session_date": str(session_date) if session_date else None,
            "note": note}


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


def get_universe(cap=60, pinned=None):
    """Universo del día. Intercala los screeners para que TODOS aporten:
    antes day_gainers llenaba el cupo y day_losers (donde caen los que se
    desploman tras correr) nunca entraba. `pinned` fuerza símbolos siempre."""
    screens = ("day_gainers", "small_cap_gainers", "aggressive_small_caps",
               "most_actives", "day_losers")
    buckets, note = [], None
    for scr in screens:
        got = []
        try:
            res = yf.screen(scr, count=25)
            quotes = res.get("quotes", []) if isinstance(res, dict) else []
            for q in quotes:
                s = (q.get("symbol") or "").upper()
                if _clean_symbol(s):
                    got.append(s)
        except Exception:
            pass
        buckets.append(got)

    syms = []
    for s in (pinned or []):
        s = s.upper().strip()
        if _clean_symbol(s) and s not in syms:
            syms.append(s)
    # round-robin: una de cada screener por vuelta
    depth = max((len(b) for b in buckets), default=0)
    for i in range(depth):
        for b in buckets:
            if i < len(b) and b[i] not in syms:
                syms.append(b[i])

    if len(syms) <= len(pinned or []):
        for s in FALLBACK_UNIVERSE:
            if s not in syms:
                syms.append(s)
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
        hi = r.get("High"); lo = r.get("Low")
        bars.append({
            "t": int(ix.timestamp()),
            "open": float(r["Open"]),
            "close": float(r["Close"]),
            "high": float(hi) if hi is not None and not pd.isna(hi) else float(r["Close"]),
            "low": float(lo) if lo is not None and not pd.isna(lo) else float(r["Close"]),
            "volume": 0 if pd.isna(v) else int(v),
        })
    return bars


# ---- volumen promedio diario (30d) con caché en memoria ----
import time as _time

_daily_cache = {}  # sym -> ({avg_vol, prev_close}, expira_ts)


def daily_stats(syms):
    """Promedio de volumen 30d y cierre de la sesión ANTERIOR, por símbolo."""
    now = _time.time()
    missing = [s for s in syms if s not in _daily_cache or _daily_cache[s][1] < now]
    if missing:
        dfd = None
        try:
            dfd = yf.download(
                tickers=missing, period="3mo", interval="1d",
                prepost=False, auto_adjust=False, progress=False,
                threads=True, group_by="ticker",
            )
        except Exception:
            pass
        today = datetime.now(tz=ET).date()
        for s in missing:
            info = {"avg_vol": None, "prev_close": None}
            try:
                if dfd is not None and not dfd.empty:
                    sub = dfd[s] if isinstance(dfd.columns, pd.MultiIndex) else dfd
                    vols = sub["Volume"].dropna()
                    closes = sub["Close"].dropna()
                    if len(vols) >= 2:
                        # 30 sesiones previas, excluyendo la barra de hoy si existe
                        hist = vols.iloc[:-1] if vols.index[-1].date() == today else vols
                        info["avg_vol"] = float(hist.iloc[-30:].mean()) if len(hist) else None
                    elif len(vols) == 1:
                        info["avg_vol"] = float(vols.mean())
                    if len(closes) >= 1:
                        if closes.index[-1].date() == today and len(closes) >= 2:
                            info["prev_close"] = float(closes.iloc[-2])
                        else:
                            info["prev_close"] = float(closes.iloc[-1])
            except Exception:
                pass
            _daily_cache[s] = (info, now + 1800)
    return {s: _daily_cache[s][0] for s in syms}


def avg_daily_volumes(syms):
    st = daily_stats(syms)
    return {s: st[s]["avg_vol"] for s in syms}


# Fracción del volumen diario operada en cada bloque de 30 min (curva U típica del mercado US).
_VOL_CURVE_30 = [13.0, 9.0, 7.5, 6.0, 5.0, 4.5, 4.5, 5.0, 5.5, 6.0, 7.0, 9.0, 18.0]


def expected_vol_frac(mins_elapsed):
    """Qué fracción del volumen diario debería haberse operado tras N minutos de sesión.
    Lineal sobrestima el RVOL temprano: a los 25 min lo real es ~11%, no 6.4%."""
    if mins_elapsed <= 0:
        return 0.0
    if mins_elapsed >= 390:
        return 1.0
    cum = 0.0
    rem = float(mins_elapsed)
    for bucket in _VOL_CURVE_30:
        if rem >= 30:
            cum += bucket
            rem -= 30
        else:
            cum += bucket * (rem / 30.0)
            break
    return cum / 100.0


def session_metrics(bars):
    """VWAP de la sesión, extensión del precio vs VWAP y estructura de mínimos crecientes."""
    cum_pv = cum_v = 0.0
    for b in bars:
        tp = (b["high"] + b["low"] + b["close"]) / 3.0
        cum_pv += tp * b["volume"]
        cum_v += b["volume"]
    vwap = cum_pv / cum_v if cum_v > 0 else None
    last = bars[-1]["close"] if bars else None
    ext = (last - vwap) / vwap * 100 if (vwap and last) else None

    swings = []
    for i in range(2, len(bars) - 2):
        lo = bars[i]["low"]
        neigh = [bars[j]["low"] for j in (i - 2, i - 1, i + 1, i + 2)]
        if all(lo < x for x in neigh):
            swings.append(lo)
    higher_lows = None
    if len(swings) >= 2:
        tail = swings[-3:]
        higher_lows = all(tail[q] >= tail[q - 1] * 0.999 for q in range(1, len(tail)))
    return {"vwap": round(vwap, 4) if vwap else None,
            "ext": round(ext, 2) if ext is not None else None,
            "higher_lows": higher_lows,
            "swings": len(swings)}


def whale_metrics(events):
    """Huella institucional: dólares movidos en eventos, impacto por dólar y bloques."""
    if not events:
        return {"dvol": 0.0, "impact": None, "blocks": 0, "tier": "baja"}
    dvol = sum(e["vol"] * e["price"] for e in events)
    move = sum(abs(e["chg"]) for e in events)
    impact = round(move / (dvol / 1e7), 2) if dvol > 0 else None  # % por cada $10M
    # bloque = un solo minuto con $10M+ cruzados (tamaño institucional por definición)
    blocks = sum(1 for e in events if e["vol"] * e["price"] >= 1e7)
    if dvol >= 25e6 or blocks >= 1 or (dvol >= 1e7 and len(events) >= 3):
        tier = "alta"
    elif dvol >= 3e6:
        tier = "media"
    else:
        tier = "baja"
    return {"dvol": round(dvol), "impact": impact, "blocks": blocks, "tier": tier}


@app.get("/api/discover")
def discover(k: float = 3.0, base: int = 20, min_vol: int = 100000, min_rel: float = 2.0):
    if not (1.0 <= k <= 50) or not (5 <= base <= 120) or not (0 <= min_rel <= 50):
        raise HTTPException(400, "parámetros fuera de rango")
    syms, note = get_universe()
    avgs = avg_daily_volumes(syms)
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
            day_open = bars[0]["open"]
            adv = avgs.get(s)
            per_min = adv / 390.0 if adv else None
            cum = sum(b["volume"] for b in bars)
            frac = len(bars) / 390.0
            rvol_dia = round(cum / (adv * frac), 2) if (adv and frac > 0) else None
            kept = []
            for e in detect_spikes(bars, k, base, min_vol):
                if cutoff and e["end"] < cutoff:
                    continue
                rel = round((e["vol"] / e["minutes"]) / per_min, 1) if per_min else None
                if rel is not None and rel < min_rel:
                    continue  # normal para ESTE stock, aunque sea spike local
                e["sym"] = s
                e["day_open"] = round(float(day_open), 2)
                e["rel_min"] = rel
                e["rvol_dia"] = rvol_dia
                e["dvol"] = round(e["vol"] * e["price"])
                kept.append(e)
            if kept:
                sm = session_metrics(bars)
                wm = whale_metrics(kept)
                for e in kept:
                    e["w_tier"] = wm["tier"]
                    e["w_dvol"] = wm["dvol"]
                    e["impact"] = wm["impact"]
                    e["blocks"] = wm["blocks"]
                    e["higher_lows"] = sm["higher_lows"]
                    e["ext_vwap"] = sm["ext"]
                events.extend(kept)
        except Exception:
            continue
    events.sort(key=lambda e: (-e["start"], -e["peak"]))
    return {
        "universe": len(syms), "scanned": scanned,
        "events": events[:80], "note": note,
        "recent_only": cutoff is not None,
    }
