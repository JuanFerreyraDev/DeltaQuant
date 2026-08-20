# Plan de Documentación: DeltaQuant (Bot de Arbitraje Triangular)

Complementa a `plan_bot_arbitraje_triangular.md` y `roadmap_git_bot_arbitraje.md`.

## Principio general: documentar en el momento de la decisión, no al final

Documentar al final falla por dos razones concretas, no solo por pereza:

1. **Se pierde el "por qué".** El código final muestra *qué* se hizo, pero para cuando llegás al final ya no recordás por qué descartaste la alternativa obvia (ej. por qué Redis quedó fuera del hot path, por qué las 3 patas se ejecutan en paralelo). Ese razonamiento es justamente lo más valioso de documentar en un sistema que toma decisiones automáticas con dinero real.
2. **En este proyecto específico hay un requisito de auditoría, no solo de mantenibilidad.** Si en 3 meses el bot pierde dinero en un incidente de reconciliación, vas a necesitar reconstruir "qué límites de riesgo estaban activos, con qué lógica se calibró el margen de seguridad, qué asumía el código sobre la staleness del book" — y eso hay que capturarlo cuando se decide, no reconstruirlo bajo presión después de perder plata.

Regla práctica: **si la documentación depende de una decisión que tomaste hoy, se escribe hoy.** Lo único que tiene sentido dejar para el final es documentación *de síntesis* (un README general, un diagrama consolidado) — nunca el registro de decisiones.

> **Idioma:** toda la documentación del código (docstrings, ADRs, README, runbooks, registro de calibraciones) va **en inglés**, consistente con la convención de commits/PRs en inglés del roadmap de Git. Todos los ejemplos y plantillas de este documento ya están en inglés — solo las explicaciones dirigidas a vos quedan en español.

---

## 1. Tipos de documentación y cuándo escribir cada uno

| Tipo | Cuándo | Vive en |
|---|---|---|
| **Docstrings** (funciones/clases) | En el mismo commit que el código | El propio archivo `.py` |
| **ADR** (Architecture Decision Record) | En el momento de tomar la decisión, antes o junto con implementarla | `docs/adr/` |
| **README por fase** | Al cerrar cada fase del roadmap (mismo momento del tag de Git) | `README.md` + `docs/fases/` |
| **Runbook de incidentes** | Cuando definís el mecanismo (reconciliación, circuit breaker), no cuando ocurre el primer incidente real | `docs/runbooks/` |
| **Registro de calibración de parámetros** | Cada vez que fijás o recalibrás un umbral (`safety_margin`, límites de `risk.py`) | `docs/calibrations.md` |
| **README general / portfolio** | Al final del proyecto (o de cada release mayor) | `README.md` (raíz) |
| **CHANGELOG** | Automático, por commit (ver roadmap de Git) | `CHANGELOG.md` |

---

## 2. Docstrings: estándar mínimo

No hace falta perfección, pero sí consistencia. Formato Google-style (legible y compatible con la mayoría de linters):

```python
async def evaluate_triangle(triangle: Triangle, prices: PriceState) -> Optional[Opportunity]:
    """Evaluate whether a triangle meets the net profitability threshold.

    Discards the triangle if any involved price exceeds the staleness
    threshold defined in settings.MAX_TICK_AGE_MS, even if the math
    itself comes out positive — see ADR-003.

    Args:
        triangle: Pre-generated triangle with the 3 involved pairs.
        prices: In-RAM price state, updated by the WS stream.

    Returns:
        Opportunity if net_return > 1.0 + safety_margin, None if the
        threshold isn't met or any price is stale.
    """
```

**Regla:** toda función en `core/` y `exchanges/` lleva docstring. En `tests/` y scripts auxiliares, opcional. La referencia a un ADR dentro del docstring (como en el ejemplo) es la forma de conectar "qué hace el código" con "por qué se decidió así" sin duplicar la explicación completa en cada función.

---

## 3. ADRs: el documento más importante de este proyecto

Un ADR es un archivo corto por decisión de arquitectura no trivial. Formato estándar (adaptado):

```markdown
# ADR-003: Parallel execution of the 3 FOK legs

## Status
Accepted

## Context
Sequential execution of the 3 legs leaves a time window between each
order where price can move, increasing the risk of leg 2 or 3 failing
after the previous one already executed.

## Decision
The 3 orders are dispatched with asyncio.gather, in parallel, not in
sequence.

## Consequences
- Reduces the slippage window between legs.
- Increases the probability of ending up with unplanned inventory if
  one leg fails and another doesn't — this is why reconciliation logic
  exists (see ADR-004), which wouldn't be necessary with pure
  sequential execution.
- Requires the executor to handle 3 independent async results instead
  of a simple linear flow.
```

### ADRs mínimos a escribir en este proyecto (uno por decisión, en el momento de tomarla)
- Por qué Redis queda fuera del hot path de evaluación.
- Por qué ejecución paralela de patas (y no secuencial) — y por qué eso obliga a tener reconciliación.
- Cómo se calibra `margen_seguridad` (metodología, no solo el valor final).
- Criterio de staleness del book (qué umbral, por qué ese número).
- Criterio del circuit breaker por incidentes repetidos (cuántos, en qué ventana, por qué esos valores).
- Decisión de sizing dinámico por profundidad de book vs. cap fijo (cuando se implemente, §9.4 del plan técnico).

No hace falta un framework pesado — cada ADR es un archivo `.md` de 15-20 líneas. Lo importante es que exista *antes* de que la decisión se vuelva difícil de reconstruir.

---

## 4. Registro de calibración de parámetros

Archivo vivo (`docs/calibrations.md`), se actualiza cada vez que se fija o cambia un número que afecta el comportamiento del bot. Formato tabla:

| Date | Parameter | Value | Calibration method | Commit |
|---|---|---|---|---|
| 2026-XX-XX | `safety_margin` | 0.15% | Total fee (with BNB discount) + slippage buffer estimated from `depth5`, set before running dry run | `abc1234` |
| 2026-XX-XX | `MAX_TICK_AGE_MS` | 300ms | P95 tick latency observed over 48h of passive monitoring | `def5678` |
| 2026-XX-XX | Daily loss limit | — | — | — |

Esto es lo que responde, meses después, la pregunta "¿por qué este número y no otro?" sin tener que releer el código o adivinar.

---

## 5. Runbooks de incidentes

Se escriben cuando se **diseña** el mecanismo (Fase 3, junto con `risk.py` y la reconciliación), no cuando ocurre el primer incidente real bajo presión.

Ejemplo mínimo (`docs/runbooks/reconciliation.md`):
```markdown
# Runbook: Reconciliation incident (failed leg)

## What the Telegram alert means
The bot detected that one leg of the triangle didn't execute after
another one did, and liquidated the resulting inventory at market.

## What to check first
1. `Incidents` table in SQLite: amount, symbol, timestamp.
2. Latency recorded in that cycle (was it an anomalous spike?).
3. Did the circuit breaker trip? (more than N incidents in the window)

## Actions
- If it's an isolated event: verify the kill switch did NOT trip on
  its own, confirm the net PnL of the incident (including the
  emergency liquidation) was recorded correctly.
- If it repeats: don't manually re-enable trading without reviewing
  the root cause — the circuit breaker exists to auto-stop things,
  respect it instead of forcing /resume.
```

Tener esto escrito de antemano importa porque un incidente real a las 3am con dinero involucrado no es el mejor momento para decidir con la cabeza fría qué hacer — el runbook ya decidió eso con calma.

---

## 6. README por fase vs. README general

- **`docs/fases/faseN.md`**: se escribe al cerrar cada fase (mismo momento que el tag de Git). Resume qué se implementó, qué se validó en dry run, y qué quedó pendiente o se descartó. Es la versión "diario de proyecto", útil para vos mismo más que para terceros.
- **`README.md` (raíz)**: se escribe/actualiza al final de cada release significativa (mínimo, al llegar a `v1.0.0-live`). Es la versión de síntesis — la que mostrarías si alguna vez este proyecto entra a un portfolio. Se arma resumiendo los README de fase, no escribiendo desde cero.

---

## 7. Checklist de documentación mapeado al roadmap

| Fase | Documentación a producir en esa fase |
|---|---|
| Fase 1 | Docstrings en `graph.py`, `exchanges/base.py`. ADR: diseño de la interfaz `ExchangeAdapter`. |
| Fase 2 | Docstrings en `evaluator.py`, `fees.py`. ADR: criterio de staleness. Primera entrada en `calibrations.md`. |
| Fase 3 | Docstrings en `risk.py`, `executor.py`. ADR: ejecución paralela + reconciliación. ADR: circuit breaker. Runbook de reconciliación. `docs/fases/fase3.md` con resultados del dry run. |
| Fase 4 | Docstrings en `telegram_bot.py`. Documentar comandos disponibles (`/status`, `/kill`, etc.) en el README. |
| Fase 5 | Documentar procedimiento de despliegue paso a paso (para vos mismo, si reinstalás en un VPS nuevo). Commit explícito con contexto al pasar a `DRY_RUN=False` (ya cubierto en el roadmap de Git, referenciarlo acá también). |
| Fase 6 | ADR: decisión de sizing dinámico si se implementa. README general consolidado. |

---

## 8. Qué NO vale la pena documentar

Para no caer en el extremo opuesto (documentar todo y no avanzar nunca):
- No documentar código autoexplicativo obvio (un getter simple no necesita docstring extenso).
- No mantener documentación de features experimentales descartadas fuera del ADR correspondiente (el ADR ya captura el "por qué no" — no hace falta un doc aparte).
- No escribir documentación de usuario final / marketing hasta que el proyecto esté validado con `v1.0.0-live` — antes de eso, es prematuro.
