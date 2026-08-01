"""Pruebas del motor de reglas, dimensionamiento y logging."""
import json
import os
import tempfile

import numpy as np
import pandas as pd

from escalera_checklist import (
    compute_liquidity,
    evaluate,
    log_signals,
    size_position,
    load_signals,
)

rng = np.random.default_rng(7)
results = []


def check(name, cond, detail=""):
    results.append((name, cond, detail))
    print(f"{'PASS' if cond else 'FAIL':4}  {name}  {detail}")


def build(n=60, base=100.0, vol=1_000_000.0):
    idx = pd.bdate_range("2026-01-01", periods=n)
    close = base + rng.normal(0, 0.3, n).cumsum()
    return pd.DataFrame(
        {"Open": close, "High": close + 0.8, "Low": close - 0.8,
         "Close": close, "Volume": np.full(n, vol)},
        index=idx,
    )


def apply_streak(df, days, step=1.0, vol_mult=2.0):
    d = df.copy()
    start = len(d) - days
    pc, pl = d["Close"].iloc[start - 1], d["Low"].iloc[start - 1]
    for i in range(days):
        j, c = start + i, pc + step * (i + 1)
        d.iloc[j, d.columns.get_loc("Close")] = c
        d.iloc[j, d.columns.get_loc("Low")] = pl + step * (i + 1) * 0.9
        d.iloc[j, d.columns.get_loc("High")] = c + 1.0
        d.iloc[j, d.columns.get_loc("Open")] = c - 0.5
        d.iloc[j, d.columns.get_loc("Volume")] = df["Volume"].iloc[0] * vol_mult
    return d


class NoEarnings:
    @property
    def earnings_dates(self): return None
    @property
    def calendar(self): return {}


class EarningsTomorrow:
    @property
    def earnings_dates(self):
        from datetime import date, timedelta
        t = date.today() + timedelta(days=1)
        return pd.DataFrame(index=pd.DatetimeIndex([f"{t} 07:30:00"]))
    @property
    def calendar(self): return {}


# --- 1. Caso GO limpio ----------------------------------------------------
d = apply_streak(build(vol=200_000.0), 3, step=0.5)   # ADV ~ $20M
v = evaluate("TESTGO", d, equity=30000, earnings_obj=NoEarnings())
check("veredicto GO en setup limpio", v.verdict == "GO", f"verdict={v.verdict}")
check("posicion dimensionada", bool(v.position.get("ok")),
      f"notional={v.position.get('notional')}")

# --- 2. Earnings manana bloquea -------------------------------------------
v = evaluate("TESTERN", d, equity=30000, earnings_obj=EarningsTomorrow())
check("earnings manana -> BLOCK", v.verdict == "BLOCK", f"verdict={v.verdict}")
check("sin dimensionamiento si BLOCK", not v.position,
      f"position={v.position}")

# --- 3. Parabolico bloquea ------------------------------------------------
d_par = apply_streak(build(vol=200_000.0), 5, step=12.0)
v = evaluate("TESTPAR", d_par, equity=30000, earnings_obj=NoEarnings())
check("parabolico -> BLOCK", v.verdict == "BLOCK", f"verdict={v.verdict}")

# --- 4. Liquidez insuficiente bloquea -------------------------------------
d_thin = apply_streak(build(vol=500.0), 3, step=0.5)   # ADV ~ $50k
v = evaluate("TESTTHIN", d_thin, equity=30000, earnings_obj=NoEarnings())
check("ADV bajo -> BLOCK", v.verdict == "BLOCK", f"verdict={v.verdict}")
liq_rule = next(r for r in v.rules if r["name"] == "liquidez_suficiente")
check("regla de liquidez marcada como fallida", liq_rule["passed"] is False)

# --- 5. Racha corta -> WAIT, no BLOCK -------------------------------------
d_short = apply_streak(build(vol=200_000.0), 1, step=0.5)
v = evaluate("TESTWAIT", d_short, equity=30000, earnings_obj=NoEarnings())
check("racha de 1 dia -> WAIT", v.verdict == "WAIT", f"verdict={v.verdict}")

# --- 6. ETF apalancado se reconoce y avisa --------------------------------
v = evaluate("MUU", d, equity=30000, earnings_obj=NoEarnings())
check("MUU reconocido como ETF apalancado", v.is_leveraged_etf is True)
check("factor +2.0 detectado", v.leverage_factor == 2.0, f"f={v.leverage_factor}")
check("subyacente resuelto a MU", v.underlying == "MU", f"u={v.underlying}")
check("avisa asimetria del par MUU/MUD",
      any("ASIMETRICO" in w for w in v.warnings),
      f"{len(v.warnings)} avisos")

v = evaluate("MVLL", d, equity=30000, earnings_obj=NoEarnings())
check("avisa que MRVL no tiene par corto",
      any("NO es ejecutable" in w for w in v.warnings))

# --- 7. Dimensionamiento: los tres topes ----------------------------------
# Riesgo manda: ATR ancho
p = size_position(equity=30000, entry=100, atr=10, risk_pct=0.02,
                  atr_stop_mult=1.5, max_position_pct=0.35, adv_usd=1e9)
check("tope de riesgo manda con ATR ancho", p["binding_constraint"] == "riesgo",
      f"binding={p['binding_constraint']} notional={p['notional']}")
# risk_amount=600, stop_distance=15, shares=40, notional=4000
check("calculo de riesgo correcto", abs(p["notional"] - 4000) < 1,
      f"notional={p['notional']}")

# Concentracion manda: ATR estrecho
p = size_position(equity=30000, entry=100, atr=0.2, risk_pct=0.02,
                  atr_stop_mult=1.5, max_position_pct=0.35, adv_usd=1e9)
check("tope de concentracion manda con ATR estrecho",
      p["binding_constraint"] == "concentracion",
      f"binding={p['binding_constraint']} notional={p['notional']}")
check("concentracion = 35% del equity", abs(p["notional"] - 10500) < 1)

# Liquidez manda
p = size_position(equity=30000, entry=100, atr=0.2, risk_pct=0.02,
                  atr_stop_mult=1.5, max_position_pct=0.35, adv_usd=200_000)
check("tope de liquidez manda con ADV bajo",
      p["binding_constraint"] == "liquidez",
      f"binding={p['binding_constraint']} notional={p['notional']}")
check("liquidez = 1% del ADV", abs(p["notional"] - 2000) < 1)

# Exposicion economica con apalancamiento
p = size_position(equity=30000, entry=100, atr=10, leverage_factor=2.0, adv_usd=1e9)
check("exposicion economica = notional * factor",
      abs(p["economic_exposure"] - p["notional"] * 2) < 1,
      f"notional={p['notional']} exp={p['economic_exposure']}")

# Stop invalido
p = size_position(equity=30000, entry=10, atr=20, adv_usd=1e9)
check("ATR mayor que el precio -> rechazado", p["ok"] is False, p.get("reason", ""))

# --- 8. Liquidez ----------------------------------------------------------
liq = compute_liquidity(build(vol=100_000.0))
check("ADV calculado", liq["ok"] and liq["adv_usd"] > 0, f"adv={liq['adv_usd']:,.0f}")

# --- 9. Log de senales ----------------------------------------------------
with tempfile.TemporaryDirectory() as tmp:
    p = os.path.join(tmp, "sig.jsonl")
    vs = [
        {"symbol": "A", "verdict": "GO", "streak": {"last_close": 10, "streak_days": 3,
         "avg_rvol_streak": 2.0, "extension_atr": 1.0, "atr": 0.5},
         "leverage_factor": 2.0, "liquidity": {"adv_usd": 1e7}},
        {"symbol": "B", "verdict": "BLOCK", "streak": {}},
        {"symbol": "C", "verdict": "WAIT", "streak": {"last_close": 5, "streak_days": 1}},
    ]
    n = log_signals(vs, path=p)
    check("BLOCK no se registra", n == 2, f"escritas={n}")
    df = load_signals(p)
    check("log releible como DataFrame", len(df) == 2 and "symbol" in df.columns)
    log_signals(vs, path=p)
    check("el log hace append", len(load_signals(p)) == 4)

print("\n" + "=" * 60)
failed = [n for n, c, _ in results if not c]
print(f"{len(results) - len(failed)}/{len(results)} pruebas pasaron")
if failed:
    print("FALLARON:", failed)
    raise SystemExit(1)
print("Motor de reglas, topes de tamano y logging validados.")
