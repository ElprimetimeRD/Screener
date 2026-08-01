# Escalera Ascendente

Verificador de disciplina para day trading de ETFs 2x de acción única.

**No es un buscador de oportunidades.** Se le pasa una watchlist corta —
los tickers que ya conoces — y responde si el setup cumple tus reglas.
El diseño es deliberado: el edge original estaba en ejecutar un patrón
estrecho con disciplina, no en encontrar más candidatos.

---

## Archivos

| Archivo | Qué hace |
|---|---|
| `escalera_signals.py` | Detector de rachas (higher lows + RVOL) y filtro de earnings |
| `leveraged_map.py` | Catálogo verificado de ETFs 2x long/short con factores explícitos |
| `escalera_checklist.py` | Motor de reglas, liquidez, dimensionamiento y log de señales |
| `app.py` | API FastAPI |
| `test_escalera.py` | 20 pruebas de la lógica de racha |
| `test_checklist.py` | 25 pruebas del motor de reglas y dimensionamiento |

## Instalación

```bash
pip install fastapi uvicorn yfinance pandas numpy
uvicorn app:app --reload
```

Render: `uvicorn app:app --host 0.0.0.0 --port $PORT`

## Endpoints

```
GET /scan?equity=30000              checklist completo, ordenado GO/WAIT/BLOCK
GET /scan?symbols=MUU,SNXX,MVLL     watchlist propia
GET /streak                         solo rachas, sin reglas
GET /pair/MU                        par 2x y avisos de asimetría
GET /pairs                          mapa completo de pares ejecutables
GET /size?equity=30000&entry=100&atr=5&symbol=MUU
GET /signals/performance            win rate real vs base rate
GET /universe?kind=short
```

## El checklist

**Reglas duras** (fallar una da `BLOCK`):

- `sin_riesgo_earnings` — no reporta hoy AMC ni mañana
- `no_parabolico` — extensión desde la SMA20 bajo el umbral en unidades de ATR
- `liquidez_suficiente` — volumen en dólares sobre el mínimo
- `datos` — historial suficiente

**Reglas blandas** (fallar una da `WAIT`):

- `higher_lows_confirmados` — días consecutivos con mínimo creciente
- `rvol_sostenido` — RVOL mínimo de la racha sobre el umbral

Una regla de tu metodología **no** es verificable aquí: *no promediar a la
baja* depende de tu ejecución, no del precio. El módulo no finge medirla.

## Dimensionamiento

Tres topes independientes; manda el menor:

1. **Riesgo** — `(equity × risk_pct) / (ATR × stop_mult)`
2. **Concentración** — `max_position_pct × equity`
3. **Liquidez** — `max_pct_of_adv × ADV`

La liquidez se calcula sobre el **percentil 25** del volumen en dólares, no
sobre la media. Un instrumento normalmente delgado con tres días de volumen
enorme —justo el caso de una racha— muestra una media alta aunque en un día
corriente no puedas deshacer la posición al tamaño que entraste.

`economic_exposure` reporta `notional × factor`: en un 2x, $10k de posición
son $20k de exposición al subyacente.

## Medición del win rate

`/scan` registra cada señal GO/WAIT en `signals.jsonl`.
`/signals/performance` compara después el resultado real contra la **base
rate** del propio ticker (probabilidad de un día alcista cualquiera).

`edge_vs_base_pct` es el número que importa. Si no es positivo después de
una muestra decente, el screener no está aportando y conviene saberlo antes
de dimensionar con él.

## Mantenimiento

El catálogo de `leveraged_map.py` está verificado al **2026-08-01**. Este
universo cambia rápido — lanzamientos, cierres, splits. Revisar
trimestralmente contra las páginas de producto de los emisores.

## Lo que no está probado contra datos reales

Toda la lógica de cálculo (racha, ATR, extensión, dimensionamiento, reglas,
logging) está validada con 45 pruebas sobre datos sintéticos. Lo que **no**
se pudo probar es la descarga de Yahoo Finance: `fetch_history()` y la
consulta de earnings están escritas con cuidado y con manejo de fallos, pero
validadas solo con mocks. Si algo falla en producción, es ahí.

Dos limitaciones conocidas de la fuente de datos:

- **Latencia**: Yahoo va 1–2 minutos atrasado. Para entradas en los
  primeros minutos de la sesión eso es material.
- **Earnings**: yfinance a veces no devuelve fecha. Cuando pasa,
  `has_event` queda en `False` pero el campo `note` lo dice explícito.
  Ausencia de dato no es ausencia de earnings — no tomarlo como vía libre.
