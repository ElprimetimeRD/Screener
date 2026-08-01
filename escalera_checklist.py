"""
escalera_checklist.py
=====================
Motor de reglas de la escalera ascendente. Integra:

  escalera_signals.py  -> racha, RVOL, extension, earnings
  leveraged_map.py     -> par 2x largo/corto, factor, asimetria

y anade lo que faltaba:

  - Chequeo de LIQUIDEZ (critico: los 2x de accion unica son delgados
    y tus boletos llegaron a $22k-27k)
  - Dimensionamiento por distancia al stop, ajustado por apalancamiento
  - LOG DE SENALES en JSONL para medir despues el win rate real

Sobre el log: es la pieza que responde la pregunta de fondo -- si estas
herramientas realmente ayudan o no. Sin medicion, cualquier screener se
siente util. Con medicion, se sabe.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Any, Optional

import pandas as pd

from escalera_signals import (
    DEFAULTS,
    EarningsRisk,
    StreakResult,
    compute_streak,
    fetch_history,
    get_earnings_risk,
)
from leveraged_map import check_pair, get_etf, get_underlying, is_leveraged
from storage import signal_record, store

SIGNAL_LOG = os.environ.get("ESCALERA_LOG", "signals.jsonl")

# ---------------------------------------------------------------------------
# Parametros de riesgo
# ---------------------------------------------------------------------------

RISK_DEFAULTS = {
    "risk_pct": 0.02,           # % del equity arriesgado por operacion
    "atr_stop_mult": 1.5,       # stop = entrada - k*ATR
    "max_position_pct": 0.35,   # techo duro de una posicion vs equity
    "max_pct_of_adv": 0.01,     # tu orden no debe pasar 1% del volumen medio
    "min_adv_usd": 2_000_000,   # volumen medio minimo en USD para operar
}


@dataclass
class Rule:
    name: str
    passed: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Verdict:
    symbol: str
    verdict: str = "BLOCK"          # GO | WAIT | BLOCK
    underlying: Optional[str] = None
    is_leveraged_etf: bool = False
    leverage_factor: Optional[float] = None

    rules: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    streak: dict = field(default_factory=dict)
    earnings: dict = field(default_factory=dict)
    liquidity: dict = field(default_factory=dict)
    pair: dict = field(default_factory=dict)
    position: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# LIQUIDEZ
# ---------------------------------------------------------------------------

def compute_liquidity(df: pd.DataFrame, window: int = 20) -> dict[str, Any]:
    """
    Volumen medio en dolares. En 2x de accion unica esto importa mas que en
    acciones: un boleto de $22k contra un ADV de $3M ya es 0.7% del volumen
    diario, y el spread se te come la ventaja del dia.

    Se reporta la MEDIA y tambien el PERCENTIL 25. La media sola engana:
    un instrumento normalmente ilíquido con tres dias de volumen enorme
    -- justo el caso de una racha -- muestra una media alta aunque en un
    dia corriente no puedas salir al tamano que entraste. El tope de
    tamano se calcula sobre el percentil bajo, no sobre la media.
    """
    d = df.dropna(subset=["Close", "Volume"])
    if len(d) < window:
        return {"ok": False, "reason": "historial insuficiente"}
    dollar_vol = (d["Close"] * d["Volume"]).tail(window)
    adv = float(dollar_vol.mean())
    p25 = float(dollar_vol.quantile(0.25))
    return {
        "ok": True,
        "adv_usd": round(adv, 2),
        "adv_p25_usd": round(p25, 2),
        "adv_usd_min": round(float(dollar_vol.min()), 2),
        "last_dollar_vol": round(float(dollar_vol.iloc[-1]), 2),
        "spike_ratio": round(adv / p25, 2) if p25 > 0 else None,
    }


def max_ticket_for_liquidity(adv_usd: float, max_pct: float) -> float:
    return round(adv_usd * max_pct, 2)


# ---------------------------------------------------------------------------
# DIMENSIONAMIENTO
# ---------------------------------------------------------------------------

def size_position(
    equity: float,
    entry: float,
    atr: float,
    leverage_factor: float = 1.0,
    risk_pct: float = RISK_DEFAULTS["risk_pct"],
    atr_stop_mult: float = RISK_DEFAULTS["atr_stop_mult"],
    max_position_pct: float = RISK_DEFAULTS["max_position_pct"],
    adv_usd: Optional[float] = None,
    max_pct_of_adv: float = RISK_DEFAULTS["max_pct_of_adv"],
) -> dict[str, Any]:
    """
    Tamano por distancia al stop, con tres topes independientes:

      1. Riesgo    -> risk_amount / stop_distance
      2. Concentracion -> max_position_pct * equity
      3. Liquidez  -> max_pct_of_adv * ADV

    Manda el MENOR de los tres. En instrumentos apalancados la distancia
    al stop ya viene amplificada por el propio ATR del ETF, asi que no se
    multiplica de nuevo; el factor se usa solo para reportar la exposicion
    economica real al subyacente.
    """
    out: dict[str, Any] = {"ok": False}
    if entry <= 0 or atr <= 0 or equity <= 0:
        out["reason"] = "entrada, ATR o equity inválidos"
        return out

    stop_distance = atr * atr_stop_mult
    stop_price = entry - stop_distance
    if stop_price <= 0:
        out["reason"] = "el stop cae en cero o negativo — ATR demasiado ancho"
        return out

    risk_amount = equity * risk_pct
    shares_by_risk = risk_amount / stop_distance
    notional_by_risk = shares_by_risk * entry

    notional_by_concentration = equity * max_position_pct
    caps = {
        "riesgo": notional_by_risk,
        "concentracion": notional_by_concentration,
    }
    if adv_usd:
        caps["liquidez"] = max_ticket_for_liquidity(adv_usd, max_pct_of_adv)

    binding = min(caps, key=caps.get)
    notional = caps[binding]
    shares = notional / entry

    out.update(
        {
            "ok": True,
            "entry": round(entry, 4),
            "stop_price": round(stop_price, 4),
            "stop_distance": round(stop_distance, 4),
            "stop_pct": round(stop_distance / entry * 100, 2),
            "risk_amount": round(risk_amount, 2),
            "shares": int(shares),
            "notional": round(notional, 2),
            "pct_of_equity": round(notional / equity * 100, 2),
            "binding_constraint": binding,
            "caps": {k: round(v, 2) for k, v in caps.items()},
            "leverage_factor": leverage_factor,
            "economic_exposure": round(notional * abs(leverage_factor), 2),
        }
    )
    if adv_usd:
        out["pct_of_adv"] = round(notional / adv_usd * 100, 3)
    return out


# ---------------------------------------------------------------------------
# MOTOR DE REGLAS
# ---------------------------------------------------------------------------

def evaluate(
    symbol: str,
    df: pd.DataFrame,
    equity: float = 0.0,
    check_earnings: bool = True,
    earnings_obj: Any = None,
    risk: Optional[dict] = None,
    **streak_params: Any,
) -> Verdict:
    """
    Aplica el checklist completo a un ticker y emite veredicto:

      GO    -> pasa todas las reglas duras
      WAIT  -> setup valido pero aun no confirmado (racha corta, RVOL bajo)
      BLOCK -> hay una razon dura para no operar (earnings, parabolico,
               liquidez insuficiente, sin datos)
    """
    rp = {**RISK_DEFAULTS, **(risk or {})}
    sym = symbol.strip().upper()
    v = Verdict(symbol=sym)

    etf = get_etf(sym)
    v.is_leveraged_etf = etf is not None
    v.leverage_factor = etf.factor if etf else None
    v.underlying = get_underlying(sym) or sym

    # --- Racha -------------------------------------------------------------
    sr: StreakResult = compute_streak(df, symbol=sym, **streak_params)
    v.streak = sr.to_dict()
    if not sr.ok:
        v.verdict = "BLOCK"
        v.rules.append(Rule("datos", False, sr.reason).to_dict())
        return v

    min_streak = streak_params.get("min_streak", DEFAULTS["min_streak"])
    min_rvol = streak_params.get("min_rvol", DEFAULTS["min_rvol"])

    v.rules.append(
        Rule(
            "higher_lows_confirmados",
            sr.streak_days >= min_streak,
            f"{sr.streak_days} días consecutivos (mínimo {min_streak})",
        ).to_dict()
    )
    v.rules.append(
        Rule(
            "rvol_sostenido",
            bool(sr.rvol_ok),
            f"RVOL mínimo de la racha {sr.min_rvol_streak:.2f}× (umbral {min_rvol}×)"
            if sr.streak_days
            else "sin racha activa",
        ).to_dict()
    )
    v.rules.append(
        Rule(
            "no_parabolico",
            not sr.parabolic,
            f"extensión {sr.extension_atr:.2f} ATR sobre la SMA",
        ).to_dict()
    )

    # --- Liquidez ----------------------------------------------------------
    liq = compute_liquidity(df)
    v.liquidity = liq
    if liq.get("ok"):
        # Se exige sobre el percentil 25, no sobre la media: un pico de
        # volumen durante la racha no debe habilitar un tamano que no
        # puedas deshacer en un dia normal.
        floor_usd = liq["adv_p25_usd"]
        liq_ok = floor_usd >= rp["min_adv_usd"]
        v.rules.append(
            Rule(
                "liquidez_suficiente",
                liq_ok,
                f"ADV medio ${liq['adv_usd']:,.0f} / p25 ${floor_usd:,.0f} "
                f"(mínimo ${rp['min_adv_usd']:,.0f}); boleto máximo "
                f"${max_ticket_for_liquidity(floor_usd, rp['max_pct_of_adv']):,.0f}",
            ).to_dict()
        )
        if liq.get("spike_ratio") and liq["spike_ratio"] >= 3:
            v.warnings.append(
                f"Volumen distorsionado por un pico: la media es {liq['spike_ratio']:.1f}× "
                f"el percentil 25. En un día normal la liquidez es mucho menor."
            )
    else:
        v.rules.append(Rule("liquidez_suficiente", False, liq.get("reason", "")).to_dict())

    # --- Earnings ----------------------------------------------------------
    if check_earnings:
        target = v.underlying if v.is_leveraged_etf else sym
        try:
            er: EarningsRisk = get_earnings_risk(target, ticker_obj=earnings_obj)
        except Exception as exc:
            er = EarningsRisk(symbol=target, note=f"error consultando earnings: {exc}")
        v.earnings = er.to_dict()
        v.rules.append(
            Rule("sin_riesgo_earnings", not er.blocks_overnight, er.note).to_dict()
        )
        if v.is_leveraged_etf and er.has_event:
            v.warnings.append(
                f"El earnings es de {target}, el subyacente de {sym}. "
                f"Un gap de 15% en {target} son ~{15 * abs(v.leverage_factor or 1):.0f}% en {sym}."
            )
    else:
        v.rules.append(Rule("sin_riesgo_earnings", True, "chequeo desactivado").to_dict())

    # --- Par apalancado ----------------------------------------------------
    if v.is_leveraged_etf:
        pair = check_pair(v.underlying)
        v.pair = pair
        v.warnings.extend(pair.get("warnings", []))

    # --- Veredicto ---------------------------------------------------------
    hard_blocks = {"sin_riesgo_earnings", "no_parabolico", "liquidez_suficiente", "datos"}
    failed = [r["name"] for r in v.rules if not r["passed"]]

    if any(f in hard_blocks for f in failed):
        v.verdict = "BLOCK"
    elif failed:
        v.verdict = "WAIT"
    else:
        v.verdict = "GO"

    # --- Dimensionamiento (solo si hay luz verde y equity) -----------------
    if v.verdict == "GO" and equity > 0:
        v.position = size_position(
            equity=equity,
            entry=sr.last_close,
            atr=sr.atr,
            leverage_factor=abs(v.leverage_factor or 1.0),
            risk_pct=rp["risk_pct"],
            atr_stop_mult=rp["atr_stop_mult"],
            max_position_pct=rp["max_position_pct"],
            adv_usd=liq.get("adv_p25_usd") or liq.get("adv_usd"),
            max_pct_of_adv=rp["max_pct_of_adv"],
        )

    return v


# ---------------------------------------------------------------------------
# SCAN COMPLETO
# ---------------------------------------------------------------------------

DEFAULT_WATCHLIST = [
    # Subyacentes de memoria/semis que conoces
    "MU", "SNDK", "MRVL", "ARM", "AXTI", "LITE", "BE", "WDC",
    # Sus vehiculos 2x
    "MUU", "SNXX", "MVLL", "AXTX", "LITX", "BEG", "ARMG", "WDCX",
    # Pares cortos ejecutables
    "SNDQ", "LITZ", "BEZ",
]


def scan(
    symbols: Optional[list[str]] = None,
    equity: float = 0.0,
    check_earnings: bool = True,
    period: str = "4mo",
    log: bool = True,
    risk: Optional[dict] = None,
    **streak_params: Any,
) -> dict[str, Any]:
    """Escanea la watchlist y devuelve el resultado listo para servir."""
    syms = [s.strip().upper() for s in (symbols or DEFAULT_WATCHLIST) if s and s.strip()]
    hist = fetch_history(syms, period=period)

    verdicts: list[dict[str, Any]] = []
    for sym in syms:
        df = hist.get(sym)
        if df is None or df.empty:
            v = Verdict(symbol=sym, verdict="BLOCK")
            v.rules.append(Rule("datos", False, "sin datos de precio").to_dict())
            verdicts.append(v.to_dict())
            continue
        v = evaluate(
            sym, df, equity=equity, check_earnings=check_earnings,
            risk=risk, **streak_params
        )
        verdicts.append(v.to_dict())

    order = {"GO": 0, "WAIT": 1, "BLOCK": 2}
    verdicts.sort(
        key=lambda r: (
            order.get(r["verdict"], 3),
            -((r.get("streak") or {}).get("streak_days") or 0),
        )
    )

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(verdicts),
        "go": sum(1 for r in verdicts if r["verdict"] == "GO"),
        "wait": sum(1 for r in verdicts if r["verdict"] == "WAIT"),
        "block": sum(1 for r in verdicts if r["verdict"] == "BLOCK"),
        "results": verdicts,
    }

    if log:
        try:
            log_signals(verdicts)
        except Exception:
            pass  # el log nunca debe tumbar el scan
    return result


# ---------------------------------------------------------------------------
# LOG DE SENALES  (para medir el win rate real)
# ---------------------------------------------------------------------------

def log_signals(verdicts: list[dict[str, Any]], path: str = SIGNAL_LOG) -> int:
    """
    Registra las senales GO/WAIT en el almacenamiento persistente.
    Los BLOCK no se registran: no son operaciones candidatas.
    """
    ts = datetime.now(timezone.utc).isoformat()
    records = [signal_record(v, ts) for v in verdicts if v.get("verdict") != "BLOCK"]
    return store.append(records)


def load_signals(path: str = SIGNAL_LOG) -> pd.DataFrame:
    """Lee el log completo desde el almacenamiento configurado."""
    return store.read()


def evaluate_signals(
    horizon_days: int = 1, path: str = SIGNAL_LOG, period: str = "6mo"
) -> dict[str, Any]:
    """
    Mide el desempeno real de las senales registradas contra el movimiento
    posterior del precio, y lo compara con la BASE RATE del propio ticker
    (probabilidad de un dia alcista cualquiera).

    Si el win rate de las senales no supera la base rate, el screener no
    esta aportando nada y conviene saberlo.
    """
    sig = load_signals(path)
    if sig.empty:
        return {"ok": False, "reason": "aún no hay señales registradas"}

    sig["date"] = pd.to_datetime(sig["ts"], errors="coerce").dt.date
    syms = sorted(sig["symbol"].dropna().unique().tolist())
    hist = fetch_history(syms, period=period)

    rows = []
    for _, s in sig.iterrows():
        df = hist.get(s["symbol"])
        if df is None or df.empty:
            continue
        d = df.dropna(subset=["Close"]).sort_index()
        dates = [i.date() if hasattr(i, "date") else i for i in d.index]
        try:
            pos = next(i for i, dd in enumerate(dates) if dd >= s["date"])
        except StopIteration:
            continue
        if pos + horizon_days >= len(d):
            continue
        entry = float(d["Close"].iloc[pos])
        exit_ = float(d["Close"].iloc[pos + horizon_days])
        if entry <= 0:
            continue
        rows.append(
            {
                "symbol": s["symbol"],
                "verdict": s["verdict"],
                "ret_pct": (exit_ / entry - 1) * 100,
            }
        )

    if not rows:
        return {"ok": False, "reason": "las señales aún no tienen desenlace medible"}

    res = pd.DataFrame(rows)

    # Base rate: % de dias alcistas de cada ticker en el periodo
    base_rates = {}
    for sym, df in hist.items():
        d = df.dropna(subset=["Close"])
        if len(d) > 1:
            base_rates[sym] = float((d["Close"].diff() > 0).mean() * 100)
    overall_base = round(sum(base_rates.values()) / len(base_rates), 2) if base_rates else None

    by_verdict = {}
    for verdict, grp in res.groupby("verdict"):
        by_verdict[verdict] = {
            "n": int(len(grp)),
            "win_rate": round(float((grp["ret_pct"] > 0).mean() * 100), 2),
            "avg_ret_pct": round(float(grp["ret_pct"].mean()), 3),
            "median_ret_pct": round(float(grp["ret_pct"].median()), 3),
        }

    go = by_verdict.get("GO", {})
    edge = (
        round(go["win_rate"] - overall_base, 2)
        if go and overall_base is not None
        else None
    )

    return {
        "ok": True,
        "horizon_days": horizon_days,
        "n_signals": int(len(res)),
        "by_verdict": by_verdict,
        "base_rate_pct": overall_base,
        "edge_vs_base_pct": edge,
        "verdict": (
            "sin ventaja medible sobre la base rate"
            if edge is not None and edge <= 0
            else "ventaja aparente — seguir midiendo"
            if edge is not None
            else "muestra insuficiente para concluir"
        ),
    }


if __name__ == "__main__":
    import sys

    syms = sys.argv[1:] or DEFAULT_WATCHLIST
    print(json.dumps(scan(syms, equity=30000), indent=2, default=str, ensure_ascii=False))
