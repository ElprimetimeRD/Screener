"""Pruebas de la logica de racha con datos sinteticos."""
import numpy as np
import pandas as pd
from escalera_signals import compute_streak

rng = np.random.default_rng(42)


def build(n=60, base=100.0, seed_vol=1_000_000):
    """Base neutra: precio plano con ruido, volumen constante."""
    idx = pd.bdate_range("2026-01-01", periods=n)
    close = base + rng.normal(0, 0.3, n).cumsum()
    high = close + 0.8
    low = close - 0.8
    vol = np.full(n, seed_vol, dtype=float)
    return pd.DataFrame(
        {"Open": close, "High": high, "Low": low, "Close": close, "Volume": vol},
        index=idx,
    )


def apply_streak(df, days, step=2.0, vol_mult=2.0):
    """Fuerza una racha valida en los ultimos `days` dias."""
    d = df.copy()
    n = len(d)
    start = n - days
    prev_close = d["Close"].iloc[start - 1]
    prev_low = d["Low"].iloc[start - 1]
    for i in range(days):
        j = start + i
        c = prev_close + step * (i + 1)
        lo = prev_low + step * (i + 1) * 0.9
        d.iloc[j, d.columns.get_loc("Close")] = c
        d.iloc[j, d.columns.get_loc("Low")] = lo
        d.iloc[j, d.columns.get_loc("High")] = c + 1.0
        d.iloc[j, d.columns.get_loc("Open")] = c - 0.5
        d.iloc[j, d.columns.get_loc("Volume")] = 1_000_000 * vol_mult
    return d


results = []


def check(name, cond, detail=""):
    results.append((name, cond, detail))
    print(f"{'PASS' if cond else 'FAIL':4}  {name}  {detail}")


# --- 1. Racha limpia de 3 dias -------------------------------------------
d = apply_streak(build(), 3)
r = compute_streak(d, "TEST1")
check("racha de 3 dias detectada", r.streak_days == 3, f"streak={r.streak_days}")
check("higher_lows True", r.higher_lows is True)
check("rvol_ok True (vol 2x)", r.rvol_ok is True, f"min_rvol={r.min_rvol_streak:.2f}")
check("days[] tiene 3 entradas", len(r.days) == 3, f"len={len(r.days)}")

# ganancia esperada: de close base a close base + 3*step
base_close = d["Close"].iloc[-4]
expected_gain = (d["Close"].iloc[-1] / base_close - 1) * 100
check(
    "gain_pct_streak correcto",
    abs(r.gain_pct_streak - expected_gain) < 1e-6,
    f"got={r.gain_pct_streak:.4f} exp={expected_gain:.4f}",
)

# --- 2. Racha rota en el ultimo dia --------------------------------------
d = apply_streak(build(), 3)
d.iloc[-1, d.columns.get_loc("Close")] = d["Close"].iloc[-2] - 1.0  # dia bajista
r = compute_streak(d, "TEST2")
check("ultimo dia bajista -> streak 0", r.streak_days == 0, f"streak={r.streak_days}")
check("escalera_ok False", r.escalera_ok is False)

# --- 3. Volumen bajo rompe la racha --------------------------------------
d = apply_streak(build(), 4)
d.iloc[-2, d.columns.get_loc("Volume")] = 100.0  # muy por debajo de la MA
r = compute_streak(d, "TEST3")
check("volumen bajo corta la racha a 1", r.streak_days == 1, f"streak={r.streak_days}")

# --- 4. Lower low rompe la racha -----------------------------------------
d = apply_streak(build(), 4)
d.iloc[-2, d.columns.get_loc("Low")] = d["Low"].iloc[-3] - 5.0
r = compute_streak(d, "TEST4")
check("lower low corta la racha a 1", r.streak_days == 1, f"streak={r.streak_days}")

# --- 5. RVOL por debajo del umbral ---------------------------------------
d = apply_streak(build(), 3, vol_mult=1.05)  # apenas sobre la media
r = compute_streak(d, "TEST5", min_rvol=1.5)
check("racha existe pero rvol_ok False", r.streak_days == 3 and r.rvol_ok is False,
      f"streak={r.streak_days} min_rvol={r.min_rvol_streak:.2f}")
check("escalera_ok False por RVOL", r.escalera_ok is False)

# --- 6. Deteccion de parabolico ------------------------------------------
d = apply_streak(build(), 5, step=12.0)  # movimiento enorme
r = compute_streak(d, "TEST6")
check("parabolico detectado", r.parabolic is True, f"ext={r.extension_atr:.2f} ATR")
check("escalera_ok False por parabolico", r.escalera_ok is False)

d = apply_streak(build(), 3, step=0.6)  # movimiento contenido
r = compute_streak(d, "TEST7")
check("movimiento contenido no es parabolico", r.parabolic is False,
      f"ext={r.extension_atr:.2f} ATR")
check("escalera_ok True (caso completo)", r.escalera_ok is True)

# --- 7. Historial insuficiente -------------------------------------------
r = compute_streak(build(n=10), "TEST8")
check("historial corto -> ok=False", r.ok is False, r.reason)

# --- 8. Columnas faltantes -----------------------------------------------
r = compute_streak(pd.DataFrame({"Close": [1, 2, 3]}), "TEST9")
check("columnas faltantes -> ok=False", r.ok is False, r.reason)

# --- 9. Serializacion JSON (NaN -> None) ---------------------------------
import json
r = compute_streak(build(), "TEST10")  # sin racha
payload = r.to_dict()
try:
    json.dumps(payload)
    ser_ok = True
except (TypeError, ValueError) as e:
    ser_ok = False
    print(e)
check("to_dict() serializa a JSON limpio", ser_ok)
check("NaN convertido a None", payload["avg_rvol_streak"] is None,
      f"val={payload['avg_rvol_streak']}")

# --- 10. Orden ascendente forzado ----------------------------------------
d = apply_streak(build(), 3)
r_asc = compute_streak(d, "ASC")
r_desc = compute_streak(d.iloc[::-1], "DESC")  # se pasa al reves a proposito
check("ordena el indice internamente", r_asc.streak_days == r_desc.streak_days,
      f"asc={r_asc.streak_days} desc={r_desc.streak_days}")

print("\n" + "=" * 60)
failed = [n for n, c, _ in results if not c]
print(f"{len(results) - len(failed)}/{len(results)} pruebas pasaron")
if failed:
    print("FALLARON:", failed)
    raise SystemExit(1)
print("Toda la logica de racha, checklist y serializacion valida.")
