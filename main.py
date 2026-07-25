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
def radar(rvol_min: float = 2.5, move_min: float = 0.0, watch: str = "",
          pm_min: float = 3.0, min_dvol: float = 250000, min_price: float = 0.10):
    """Radar de movers. Cubre PRE-MARKET (4:00-9:30 ET) y sesión regular.
    En pre-market la señal es el volumen operado como % del volumen diario típico:
    un 5% antes de abrir ya es notable, 20%+ es explosivo. Ahí es donde se
    delatan los movimientos como el de STAK, horas antes de la campana."""
    if not (1.0 <= rvol_min <= 30) or not (0 <= move_min <= 100):
        raise HTTPException(400, "parámetros fuera de rango")
    pinned = [w for w in watch.replace(";", ",").replace(" ", ",").split(",") if w][:8]
    ckey = f"{rvol_min}|{move_min}|{watch}|{pm_min}|{min_dvol}|{min_price}"
    hit = cached_result("radar", ckey)
    if hit is not None:
        return hit  # varias pestañas comparten el mismo cálculo
    syms, note = get_universe(pinned=pinned)
    stats = daily_stats(syms)
    data = download_intraday(syms, period="1d", interval="1m",
                             prepost=True, auto_adjust=False)
    if not data:
        raise HTTPException(502, "no se pudo descargar datos intradía")

    now_et = datetime.now(tz=ET)
    mins_now = now_et.hour * 60 + now_et.minute
    weekday = now_et.weekday() < 5
    live = (SESSION_OPEN_MIN <= mins_now <= SESSION_CLOSE_MIN) and weekday
    premarket = (PREMARKET_OPEN_MIN <= mins_now < SESSION_OPEN_MIN) and weekday

    cands = []
    session_date = None
    for s in syms:
        try:
            bars = data.get(s)
            if not bars or len(bars) < 2:
                continue
            by_date = {}
            for b in bars:
                et = datetime.fromtimestamp(b["t"], tz=ET)
                m = et.hour * 60 + et.minute
                if PREMARKET_OPEN_MIN <= m <= SESSION_CLOSE_MIN:
                    by_date.setdefault(et.date(), []).append((m, b))
            if not by_date:
                continue
            d = max(by_date)
            allb = by_date[d]
            pre = [b for m, b in allb if m < SESSION_OPEN_MIN]
            sess = [b for m, b in allb if m >= SESSION_OPEN_MIN]
            if session_date is None or d > session_date:
                session_date = d

            st = stats.get(s) or {}
            adv = st.get("avg_vol")
            prev_close = st.get("prev_close")

            pre_vol = sum(b["volume"] for b in pre)
            # volumen pre-market como % del día típico: la señal temprana clave
            pm_pct = round(pre_vol / adv * 100, 1) if adv else None
            pre_last = pre[-1]["close"] if pre else None
            pm_chg = round((pre_last - prev_close) / prev_close * 100, 2) if (pre_last and prev_close) else None

            in_pre = premarket or not sess
            if in_pre and pre:
                last = pre_last
                day_open = pre[0]["open"]
                elapsed = len(pre)
                rvol = None  # en pre-market manda pm_pct, no el RVOL de sesión
                accel = None
                if elapsed >= 10:
                    r5 = sum(b["volume"] for b in pre[-5:])
                    p5 = sum(b["volume"] for b in pre[-10:-5])
                    accel = round(r5 / p5, 1) if p5 > 0 else None
            elif sess:
                last = sess[-1]["close"]
                day_open = sess[0]["open"]
                elapsed = len(sess)
                cum = sum(b["volume"] for b in sess)
                frac = expected_vol_frac(elapsed)
                rvol = cum / (adv * frac) if (adv and frac > 0) else None
                accel = None
                if elapsed >= 10:
                    r5 = sum(b["volume"] for b in sess[-5:])
                    p5 = sum(b["volume"] for b in sess[-10:-5])
                    accel = round(r5 / p5, 1) if p5 > 0 else None
            else:
                continue

            gap = round((day_open - prev_close) / prev_close * 100, 2) if prev_close else None
            chg_total = round((last - prev_close) / prev_close * 100, 2) if prev_close else None
            chg_open = round((last - day_open) / day_open * 100, 2) if day_open else None
            move = abs(chg_total) if chg_total is not None else abs(chg_open or 0)

            is_pinned = s in [p.upper() for p in pinned]
            phase = "pre" if in_pre else "reg"
            phase_vol = sum(b["volume"] for b in (pre if in_pre else sess))
            dollar_vol = phase_vol * (last or 0)
            if not is_pinned:
                # descarta sub-penny y volumen irrisorio: 3 acciones de un ticker
                # a $0.0001 producen RVOL de 300x que no significa nada
                if (last or 0) < min_price or dollar_vol < min_dvol:
                    continue
                if phase == "pre":
                    # en pre-market: volumen relevante vs su día típico + movimiento
                    if (pm_pct or 0) < pm_min or move < 2:
                        continue
                else:
                    if rvol is None or rvol < rvol_min:
                        continue
                    if move < move_min:
                        continue

            rv = rvol or 0
            if phase == "pre":
                if (pm_pct or 0) >= 15 and move >= 10:
                    grade = "caliente"
                elif (pm_pct or 0) >= 5 and move >= 4:
                    grade = "activo"
                else:
                    grade = "vigilar"
            else:
                if rv >= 5 and (accel or 0) >= 1.5 and move >= 3:
                    grade = "caliente"
                elif rv >= 3 and ((accel or 0) >= 1.5 or move >= 2):
                    grade = "activo"
                else:
                    grade = "vigilar"

            stair = None
            try:
                stair = staircase_metrics(sess if not in_pre else pre)
            except Exception:
                stair = None
            cands.append({
                "stair": stair,
                "sym": s, "price": round(float(last), 2),
                "rvol": round(rvol, 1) if rvol else None, "accel": accel,
                "gap": gap, "chg_total": chg_total, "chg_open": chg_open,
                "elapsed": elapsed, "dvol": round(dollar_vol),
                "grade": grade, "pinned": is_pinned, "phase": phase,
                "pm_pct": pm_pct, "pm_chg": pm_chg, "pm_vol": int(pre_vol),
            })
        except Exception:
            continue

    order = {"caliente": 0, "activo": 1, "vigilar": 2}
    def sort_key(c):
        strength = -(c["pm_pct"] or 0) if c["phase"] == "pre" else -(c["rvol"] or 0)
        return (not c.get("pinned"), order[c["grade"]], strength)
    cands.sort(key=sort_key)
    del data
    gc.collect()
    result = {"universe": len(syms), "candidates": cands[:40],
            "mins_into_session": max(0, mins_now - SESSION_OPEN_MIN) if live else 390,
            "live": live, "premarket": premarket,
            "session_date": str(session_date) if session_date else None,
            "note": note}
    store_result("radar", ckey, result)
    return result


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
PREMARKET_OPEN_MIN = 4 * 60          # 4:00 AM ET
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


def _explosive_movers(limit=40):
    """Consulta directa a Yahoo por movimiento explosivo, en ambas direcciones.
    No depende de que el símbolo esté en una lista predefinida: pregunta por
    'todo lo que se movió fuerte con volumen', que es justo lo que busca el radar."""
    out = []
    for asc, field in ((False, "percentchange"), (True, "percentchange")):
        try:
            q = yf.EquityQuery("and", [
                yf.EquityQuery("gt", ["dayvolume", 200000]),
                yf.EquityQuery("eq", ["region", "us"]),
            ])
            res = yf.screen(q, sortField=field, sortAsc=asc, size=limit)
            quotes = res.get("quotes", []) if isinstance(res, dict) else []
            for qt in quotes:
                s = (qt.get("symbol") or "").upper()
                if _clean_symbol(s) and s not in out:
                    out.append(s)
        except Exception:
            continue
    return out


def get_universe(cap=70, pinned=None):
    """Universo del día. Primero los que MÁS se movieron (consulta directa),
    luego los screeners predefinidos intercalados para que todos aporten."""
    syms, note = [], None
    for s in (pinned or []):
        s = s.upper().strip()
        if _clean_symbol(s) and s not in syms:
            syms.append(s)

    movers = _explosive_movers()
    for s in movers:
        if s not in syms:
            syms.append(s)

    screens = ("day_gainers", "small_cap_gainers", "aggressive_small_caps",
               "most_actives", "day_losers")
    buckets = []
    for scr in screens:
        got = []
        try:
            res = yf.screen(scr, count=40)
            quotes = res.get("quotes", []) if isinstance(res, dict) else []
            for q in quotes:
                s = (q.get("symbol") or "").upper()
                if _clean_symbol(s):
                    got.append(s)
        except Exception:
            pass
        buckets.append(got)

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


@app.get("/api/universe")
def universe_debug(sym: str = ""):
    """Diagnóstico: qué símbolos trae cada fuente y si uno concreto aparece."""
    out = {"movers": [], "screens": {}, "total": 0}
    try:
        out["movers"] = _explosive_movers()
    except Exception as e:
        out["movers_error"] = str(e)
    for scr in ("day_gainers", "small_cap_gainers", "aggressive_small_caps",
                "most_actives", "day_losers"):
        try:
            res = yf.screen(scr, count=40)
            quotes = res.get("quotes", []) if isinstance(res, dict) else []
            out["screens"][scr] = [(q.get("symbol") or "").upper() for q in quotes]
        except Exception as e:
            out["screens"][scr] = ["ERROR: " + e.__class__.__name__]
    syms, note = get_universe()
    out["total"] = len(syms)
    out["universe"] = syms
    out["note"] = note
    if sym:
        s = sym.upper().strip()
        found = {"in_universe": s in syms,
                 "position": syms.index(s) + 1 if s in syms else None,
                 "in_movers": s in out["movers"],
                 "in_screens": [k for k, v in out["screens"].items() if s in v]}
        out["lookup"] = {s: found}
    return out


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


import gc

_result_cache = {}  # endpoint -> (key, data, ts)


def cached_result(name, key, ttl=20):
    ent = _result_cache.get(name)
    if ent and ent[0] == key and (_time.time() - ent[2]) < ttl:
        return ent[1]
    return None


def store_result(name, key, data):
    _result_cache[name] = (key, data, _time.time())


def download_intraday(syms, chunk=20, **kw):
    """Descarga velas por lotes y convierte cada lote a listas ligeras,
    liberando el DataFrame de inmediato. Un solo download de 110 símbolos
    reventaba los 512MB de Render; por lotes el pico baja drásticamente."""
    out = {}
    for i in range(0, len(syms), chunk):
        batch = syms[i:i + chunk]
        df = None
        try:
            df = yf.download(tickers=batch, group_by="ticker",
                             progress=False, threads=True, **kw)
            if df is None or df.empty:
                continue
            for s in batch:
                try:
                    sub = df[s] if isinstance(df.columns, pd.MultiIndex) else df
                    bars = bars_from_frame(sub)
                    if bars:
                        out[s] = bars
                except Exception:
                    continue
        except Exception:
            continue
        finally:
            del df
            gc.collect()
    return out


def daily_stats(syms):
    """Promedio de volumen 30d y cierre de la sesión ANTERIOR, por símbolo."""
    now = _time.time()
    missing = [s for s in syms if s not in _daily_cache or _daily_cache[s][1] < now]
    if missing:
        frames = {}
        for i in range(0, len(missing), 25):
            batch = missing[i:i + 25]
            try:
                d = yf.download(tickers=batch, period="3mo", interval="1d",
                                prepost=False, auto_adjust=False, progress=False,
                                threads=True, group_by="ticker")
                for s in batch:
                    try:
                        sub = d[s] if isinstance(d.columns, pd.MultiIndex) else d
                        frames[s] = sub[["Close", "Volume"]].copy()
                    except Exception:
                        pass
                del d
                gc.collect()
            except Exception:
                continue
        today = datetime.now(tz=ET).date()
        for s in missing:
            info = {"avg_vol": None, "prev_close": None}
            try:
                sub = frames.get(s)
                if sub is not None and not sub.empty:
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


def staircase_metrics(bars, tol=0.998):
    """Clasifica la ESTRUCTURA del movimiento desde velas de 1m, para juzgarlo
    sin abrir el gráfico:
      escalera   -> mínimos crecientes, retrocesos superficiales, avance limpio
      parabolica -> sube vertical sin corregir (la que NO hay que perseguir)
      irregular  -> sube pero sin estructura confiable
    """
    if len(bars) < 12:
        return None
    lows = [b["low"] for b in bars]
    highs = [b["high"] for b in bars]
    closes = [b["close"] for b in bars]
    n_all = len(bars)

    swings = []
    for i in range(2, n_all - 2):
        lo = lows[i]
        if lo < lows[i-2] and lo < lows[i-1] and lo < lows[i+1] and lo < lows[i+2]:
            swings.append((i, lo))
    # confirmación relajada cerca del final: un mínimo fresco cuenta 2 min antes
    for i in (n_all - 3, n_all - 2):
        if i > 2 and lows[i] < lows[i-1] and lows[i] < lows[i-2] and lows[i] < lows[min(i+1, n_all-1)]:
            if not any(abs(i - j) <= 1 for j, _ in swings):
                swings.append((i, lows[i]))
    # el origen del tramo (mínimo absoluto) siempre es un peldaño válido
    origin = min(range(n_all), key=lambda i: lows[i])
    if not any(abs(origin - j) <= 1 for j, _ in swings):
        swings.append((origin, lows[origin]))
    swings.sort()
    if not swings:
        return None

    anchor = min(range(len(swings)), key=lambda k: swings[k][1])
    seq = swings[anchor:]
    legs = 1
    for k in range(1, len(seq)):
        if seq[k][1] >= seq[k-1][1] * tol:
            legs += 1
        else:
            break

    start_i, start_px = seq[0]
    last_px = closes[-1]
    if last_px <= start_px or start_i >= n_all - 4:
        return None
    gain = (last_px - start_px) / start_px * 100
    climb_mins = n_all - start_i

    peak, max_dd = start_px, 0.0
    for i in range(start_i, n_all):
        peak = max(peak, highs[i])
        dd = (peak - lows[i]) / peak * 100 if peak else 0
        max_dd = max(max_dd, dd)

    seg = closes[start_i:]
    n = len(seg)
    if n < 5:
        return None
    mx, my = (n - 1) / 2.0, sum(seg) / n
    sxy = sum((i - mx) * (seg[i] - my) for i in range(n))
    sxx = sum((i - mx) ** 2 for i in range(n))
    syy = sum((seg[i] - my) ** 2 for i in range(n))
    if sxx <= 0 or syy <= 0 or sxy <= 0:
        return None
    r2 = (sxy * sxy) / (sxx * syy)
    ratio = gain / max_dd if max_dd > 0.05 else 99.0

    if legs >= 2 and r2 >= 0.5 and ratio >= 2.0:
        kind = "escalera"
    elif gain >= 5 and max_dd < gain * 0.08 and r2 >= 0.7:
        kind = "parabolica"
    else:
        kind = "irregular"

    return {"kind": kind, "legs": legs, "climb_mins": climb_mins,
            "gain": round(gain, 2), "pullback": round(max_dd, 2),
            "r2": round(r2, 2), "ratio": round(min(ratio, 99), 1),
            "early": climb_mins <= 45 and gain <= 40}


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
    dkey = f"{k}|{base}|{min_vol}|{min_rel}"
    hit = cached_result("discover", dkey)
    if hit is not None:
        return hit
    syms, note = get_universe()
    avgs = avg_daily_volumes(syms)
    data = download_intraday(syms, period="1d", interval="1m",
                             prepost=False, auto_adjust=False)
    if not data:
        raise HTTPException(502, "no se pudo descargar datos intradía")

    now_et = datetime.now(tz=ET)
    mins = now_et.hour * 60 + now_et.minute
    cutoff = None
    if SESSION_OPEN_MIN <= mins <= SESSION_CLOSE_MIN and now_et.weekday() < 5:
        cutoff = int(datetime.now(timezone.utc).timestamp()) - 45 * 60

    events, scanned = [], 0
    for s in syms:
        try:
            bars = data.get(s)
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
    del data
    gc.collect()
    result = {
        "universe": len(syms), "scanned": scanned,
        "events": events[:80], "note": note,
        "recent_only": cutoff is not None,
    }
    store_result("discover", dkey, result)
    return result
