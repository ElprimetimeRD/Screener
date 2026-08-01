"""
leveraged_map.py
================
Mapa verificado subyacente <-> ETF apalancado de accion unica.

POR QUE ESTE MODULO NO ES UNA SIMPLE TABLA
-------------------------------------------
Al verificar los tickers contra las fuentes de los emisores aparecieron
tres problemas que una tabla ingenua "subyacente -> 2x long / 2x short"
te haria pasar por alto:

  1. EL FACTOR NO SIEMPRE ES -2x. El par corto de MU en Direxion (MUD)
     es -1x, no -2x. NVDS de Tradr es -1.5x. Si asumes simetria, tu
     tamano de posicion en el lado corto queda mal calculado por un
     factor de 2.

  2. COLISION DE TICKERS. MUU es el Direxion Daily MU Bull 2X en Nasdaq,
     PERO tambien existe un MUU en Toronto (SavvyLong 2X Micron, de
     LongPoint, constituido en junio 2026). Un proveedor de datos que
     resuelva mal el simbolo te devuelve la serie equivocada.

  3. EL LADO CORTO ES MUCHO MAS DELGADO QUE EL LARGO. Varios subyacentes
     tienen 2x long pero NO tienen 2x short. La estrategia de par
     largo/corto solo es ejecutable donde ambas patas existen.

Por eso cada entrada lleva su factor explicito, su emisor, y el mapa
expone `check_pair()` para que el screener avise antes de operar.

FECHA DE VERIFICACION: 2026-08-01
Fuentes: paginas de producto de GraniteShares, Tradr ETFs, Direxion,
Leverage Shares, REX Shares (T-REX), y registros SEC.

MANTENIMIENTO: este universo cambia rapido (lanzamientos, cierres,
splits). Revisar trimestralmente. Direxion anuncio el split de nueve
ETFs el 26 de junio de 2026 - los splits rompen la continuidad de las
series historicas de precio.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Optional

VERIFIED_AS_OF = "2026-08-01"


@dataclass(frozen=True)
class LeveragedETF:
    ticker: str
    underlying: str
    factor: float           # +2.0, -2.0, -1.5, -1.0, +1.25 ...
    issuer: str
    exchange: str = ""
    note: str = ""

    @property
    def is_long(self) -> bool:
        return self.factor > 0

    @property
    def is_short(self) -> bool:
        return self.factor < 0


# ---------------------------------------------------------------------------
# TABLA VERIFICADA
# ---------------------------------------------------------------------------
# Se prioriza cobertura de los subyacentes que operas (memoria, semis,
# optica, energia) sobre cobertura exhaustiva del mercado.

CATALOG: tuple[LeveragedETF, ...] = (
    # --- MEMORIA / ALMACENAMIENTO ------------------------------------------
    LeveragedETF("MUU",  "MU",   +2.0, "Direxion", "Nasdaq",
                 "COLISIÓN: existe otro MUU en TSX (LongPoint SavvyLong 2X Micron)"),
    LeveragedETF("MULL", "MU",   +2.0, "GraniteShares", "Nasdaq"),
    LeveragedETF("MUD",  "MU",   -1.0, "Direxion", "Nasdaq",
                 "ASIMÉTRICO: es −1×, NO −2×. No es el espejo de MUU"),
    LeveragedETF("SNXX", "SNDK", +2.0, "Tradr", "Cboe"),
    LeveragedETF("SNDG", "SNDK", +2.0, "Leverage Shares", "Cboe"),
    LeveragedETF("SNDU", "SNDK", +2.0, "REX Shares (T-REX)", ""),
    LeveragedETF("SNDQ", "SNDK", -2.0, "Tradr", "Cboe", "Inicio 22-abr-2026"),
    LeveragedETF("WDCX", "WDC",  +2.0, "Tradr", "Cboe"),
    LeveragedETF("SKUU", "SKHY", +2.0, "GraniteShares", ""),
    LeveragedETF("SKDD", "SKHY", -2.0, "GraniteShares", ""),
    LeveragedETF("SKHN", "SKHY", -2.0, "Tradr", "Cboe"),

    # --- SEMIS / COMPUTO ----------------------------------------------------
    LeveragedETF("MVLL", "MRVL", +2.0, "GraniteShares", "Nasdaq"),
    LeveragedETF("ARMG", "ARM",  +2.0, "Leverage Shares", ""),
    LeveragedETF("AXTX", "AXTI", +2.0, "Tradr", "Cboe"),
    LeveragedETF("CRDU", "CRDO", +2.0, "Tradr", "Cboe"),
    LeveragedETF("NVDL", "NVDA", +2.0, "GraniteShares", "Nasdaq"),
    LeveragedETF("NVD",  "NVDA", -2.0, "GraniteShares", "Nasdaq"),
    LeveragedETF("NVDS", "NVDA", -1.5, "Tradr", "Cboe",
                 "ASIMÉTRICO: −1.5×, no −2×"),
    LeveragedETF("AMDL", "AMD",  +2.0, "GraniteShares", "Nasdaq"),
    LeveragedETF("TSMU", "TSM",  +2.0, "GraniteShares", "Nasdaq"),
    LeveragedETF("INTW", "INTC", +2.0, "GraniteShares", "Nasdaq"),
    LeveragedETF("QCML", "QCOM", +2.0, "GraniteShares", "Nasdaq"),
    LeveragedETF("AVGU", "AVGO", +2.0, "GraniteShares", "Nasdaq"),
    LeveragedETF("SMCL", "SMCI", +2.0, "GraniteShares", "Nasdaq"),

    # --- OPTICA / REDES -----------------------------------------------------
    LeveragedETF("LITX", "LITE", +2.0, "Tradr", "Cboe", "Lumentum Holdings"),
    LeveragedETF("LITZ", "LITE", -2.0, "Tradr", "Cboe"),
    LeveragedETF("COHX", "COHR", +2.0, "Tradr", "Cboe"),
    LeveragedETF("AAOX", "AAOI", +2.0, "Tradr", "Cboe"),
    LeveragedETF("AAOZ", "AAOI", -2.0, "Tradr", "Cboe"),

    # --- ENERGIA / INFRAESTRUCTURA -----------------------------------------
    LeveragedETF("BEG",  "BE",   +2.0, "Leverage Shares", ""),
    LeveragedETF("BEZ",  "BE",   -2.0, "Tradr", "Cboe"),
    LeveragedETF("VRTL", "VRT",  +2.0, "GraniteShares", "Nasdaq"),
    LeveragedETF("SMZ",  "SMR",  -2.0, "Tradr", "Cboe"),

    # --- ESPACIO ------------------------------------------------------------
    LeveragedETF("SPAL", "SPCX", +2.0, "GraniteShares", "",
                 "SPCX es la ACCIÓN de SpaceX, no un ETF"),
    LeveragedETF("SNK",  "SPCX", -2.0, "GraniteShares", ""),
    LeveragedETF("SPCG", "SPCX", -2.0, "Tradr", "Cboe", "Inicio 12-jun-2026"),

    # --- OTROS QUE HAS TOCADO / LIQUIDOS -----------------------------------
    LeveragedETF("TSLR", "TSLA", +2.0, "GraniteShares", "Nasdaq"),
    LeveragedETF("TSDD", "TSLA", -2.0, "GraniteShares", "Nasdaq"),
    LeveragedETF("TSLQ", "TSLA", -2.0, "Tradr", "Cboe"),
    LeveragedETF("CONL", "COIN", +2.0, "GraniteShares", "Nasdaq"),
    LeveragedETF("CONI", "COIN", -2.0, "GraniteShares", "Nasdaq"),
    LeveragedETF("PTIR", "PLTR", +2.0, "GraniteShares", "Nasdaq"),
    LeveragedETF("MSTP", "MSTR", +2.0, "GraniteShares", "Nasdaq"),
    LeveragedETF("NBIL", "NBIS", +2.0, "GraniteShares", "Nasdaq"),
    LeveragedETF("NBIZ", "NBIS", -2.0, "Tradr", "Cboe"),
    LeveragedETF("APLZ", "APLD", -2.0, "Tradr", "Cboe"),
    LeveragedETF("IREZ", "IREN", -2.0, "Tradr", "Cboe"),
    LeveragedETF("ORCZ", "ORCL", -2.0, "Tradr", "Cboe"),
    LeveragedETF("AMZO", "AMZN", -2.0, "Tradr", "Cboe"),
    LeveragedETF("CBRZ", "CBRS", -2.0, "Tradr", "Cboe"),
    LeveragedETF("MRAL", "MARA", +2.0, "GraniteShares", "Nasdaq"),
    LeveragedETF("IONL", "IONQ", +2.0, "GraniteShares", "Nasdaq"),
    LeveragedETF("RDTL", "RDDT", +2.0, "GraniteShares", "Nasdaq"),
    LeveragedETF("BTDL", "BTDR", +2.0, "GraniteShares", "Nasdaq"),
)

# Indices de busqueda
_BY_TICKER: dict[str, LeveragedETF] = {e.ticker: e for e in CATALOG}
_BY_UNDERLYING: dict[str, list[LeveragedETF]] = {}
for _e in CATALOG:
    _BY_UNDERLYING.setdefault(_e.underlying, []).append(_e)


# ---------------------------------------------------------------------------
# CONSULTAS
# ---------------------------------------------------------------------------

def is_leveraged(ticker: str) -> bool:
    """True si el ticker es un ETF apalancado conocido (no el subyacente)."""
    return ticker.strip().upper() in _BY_TICKER


def get_etf(ticker: str) -> Optional[LeveragedETF]:
    return _BY_TICKER.get(ticker.strip().upper())


def get_underlying(ticker: str) -> Optional[str]:
    """Dado un ETF apalancado devuelve su subyacente. None si no lo conoce."""
    e = get_etf(ticker)
    return e.underlying if e else None


def longs_for(underlying: str) -> list[LeveragedETF]:
    """Todos los vehiculos LARGOS apalancados del subyacente."""
    return sorted(
        (e for e in _BY_UNDERLYING.get(underlying.strip().upper(), []) if e.is_long),
        key=lambda e: -e.factor,
    )


def shorts_for(underlying: str) -> list[LeveragedETF]:
    """Todos los vehiculos CORTOS apalancados del subyacente."""
    return sorted(
        (e for e in _BY_UNDERLYING.get(underlying.strip().upper(), []) if e.is_short),
        key=lambda e: e.factor,
    )


def check_pair(underlying: str) -> dict[str, Any]:
    """
    Evalua si el subyacente admite la estrategia de par largo/corto 2x.

    Devuelve un dict con:
      tradeable_pair  -> hay al menos un largo y un corto
      symmetric       -> |factor largo| == |factor corto| (mismo tamano de riesgo)
      warnings        -> lista de avisos accionables
    """
    u = underlying.strip().upper()
    longs = longs_for(u)
    shorts = shorts_for(u)

    out: dict[str, Any] = {
        "underlying": u,
        "longs": [asdict(e) for e in longs],
        "shorts": [asdict(e) for e in shorts],
        "tradeable_pair": bool(longs and shorts),
        "symmetric": False,
        "best_long": longs[0].ticker if longs else None,
        "best_short": None,
        "warnings": [],
        "verified_as_of": VERIFIED_AS_OF,
    }

    if not longs:
        out["warnings"].append(f"No hay ETF 2× largo conocido para {u}")
    if not shorts:
        out["warnings"].append(
            f"No hay ETF corto apalancado para {u} — el par largo/corto NO es ejecutable"
        )

    if longs and shorts:
        best_long = longs[0]
        # Preferir el corto cuyo factor sea espejo del largo
        mirror = [s for s in shorts if abs(s.factor) == abs(best_long.factor)]
        best_short = mirror[0] if mirror else shorts[0]
        out["best_short"] = best_short.ticker
        out["symmetric"] = abs(best_short.factor) == abs(best_long.factor)
        if not out["symmetric"]:
            out["warnings"].append(
                f"ASIMÉTRICO: {best_long.ticker} es {best_long.factor:+g}× pero "
                f"{best_short.ticker} es {best_short.factor:+g}×. "
                f"Para igualar riesgo, la pata corta necesita "
                f"{abs(best_long.factor / best_short.factor):.2f}× el capital de la larga."
            )

    # Avisos por nota del emisor (colisiones, asimetrias, etc.)
    for e in longs + shorts:
        if e.note:
            out["warnings"].append(f"{e.ticker}: {e.note}")

    return out


def position_size_short_leg(
    long_notional: float, underlying: str
) -> Optional[dict[str, Any]]:
    """
    Calcula cuanto capital necesita la pata corta para igualar la exposicion
    de la pata larga cuando los factores no son simetricos.
    """
    info = check_pair(underlying)
    if not info["tradeable_pair"]:
        return None
    lg = _BY_TICKER[info["best_long"]]
    sh = _BY_TICKER[info["best_short"]]
    ratio = abs(lg.factor / sh.factor)
    return {
        "long_ticker": lg.ticker,
        "long_notional": round(long_notional, 2),
        "short_ticker": sh.ticker,
        "short_notional": round(long_notional * ratio, 2),
        "ratio": round(ratio, 4),
        "symmetric": info["symmetric"],
    }


def universe(kind: str = "all") -> list[str]:
    """Lista de tickers del catalogo. kind: 'all' | 'long' | 'short'."""
    if kind == "long":
        return sorted(e.ticker for e in CATALOG if e.is_long)
    if kind == "short":
        return sorted(e.ticker for e in CATALOG if e.is_short)
    return sorted(e.ticker for e in CATALOG)


def underlyings() -> list[str]:
    return sorted(_BY_UNDERLYING)


def pairs_report() -> list[dict[str, Any]]:
    """Reporte completo: que subyacentes admiten par largo/corto y cuales no."""
    return sorted(
        (check_pair(u) for u in underlyings()),
        key=lambda r: (not r["tradeable_pair"], not r["symmetric"], r["underlying"]),
    )


if __name__ == "__main__":
    print(f"Catalogo verificado al {VERIFIED_AS_OF}")
    print(f"{len(CATALOG)} ETFs | {len(underlyings())} subyacentes\n")
    for r in pairs_report():
        if r["tradeable_pair"]:
            sym = "simétrico" if r["symmetric"] else "ASIMÉTRICO"
            print(f"  {r['underlying']:6} {r['best_long']:5} / {r['best_short']:5}  {sym}")
        else:
            print(f"  {r['underlying']:6} {str(r['best_long']):5} /  --    sin par ejecutable")
