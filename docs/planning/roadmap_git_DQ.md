# Roadmap de Git: DeltaQuant (Bot de Arbitraje Triangular)

Complementa a `plan_DeltaQuant.md`. Define cómo versionar el trabajo fase a fase para tener un historial profesional, revisable y con puntos de rollback claros — útil incluso siendo el único dev, porque en un proyecto que maneja capital real voy a necesitar poder auditar "qué cambió, cuándo, y por qué" cuando algo salga mal.

> **Idioma:** nombres de rama, mensajes de commit, y títulos/descripciones de PR van **en inglés** — es el estándar de facto en el ecosistema open source y de herramientas de dev (Git, GitHub, changelogs autogenerados), y mantiene el repo consistente si en algún momento se comparte o se usa como parte de un portfolio. Todos los ejemplos de este documento ya están en inglés.

---

## 1. Modelo de Branching

GitFlow simplificado (sin `release/*`, innecesario para un proyecto solo/personal):

```text
main        → siempre desplegable. Solo recibe merges desde develop, y solo
              cuando la fase correspondiente pasó DRY_RUN sin incidentes.
develop     → rama de integración. Todo el trabajo de una fase converge acá
              antes de considerarse "hecho".
feature/*   → una rama por tarea concreta del checklist de cada fase.
fix/*       → correcciones sobre algo ya mergeado a develop o main.
chore/*     → tareas de infraestructura/tooling que no son feature ni fix
              (CI, Dockerfile, dependencias).
```

**Regla dura para este proyecto en particular:** nada llega a `main` con `DRY_RUN=False` como default, y ningún merge a `main` se hace sin haber corrido esa fase en modo simulación durante el período mínimo definido en el plan (varios días para Fase 3, varios días en el VPS real para Fase 5). `main` no es "el código más nuevo", es "el código validado".

### Convención de nombres de rama
```
feature/<fase>-<descripcion-corta-en-kebab-case>
fix/<descripcion-corta>
chore/<descripcion-corta>
```
Ejemplos:
- `feature/f1-exchange-adapter-interface`
- `feature/f2-evaluator-staleness-check`
- `feature/f3-risk-daily-loss-limit`
- `fix/f3-sqlite-wal-lock-contention`
- `chore/f5-dockerfile-multistage`

---

## 2. Convención de Commits

[Conventional Commits](https://www.conventionalcommits.org/), con `scope` = módulo del proyecto (da un historial filtrable por `git log --grep` o por scope cuando se necesite auditar solo `risk` o solo `executor`, por ejemplo).

```
<type>(<scope>): <imperative, lowercase description, no trailing period>

[optional body: why, not what — the diff already shows what]
```

**Tipos:** `feat`, `fix`, `refactor`, `test`, `docs`, `chore`, `perf`

**Scopes sugeridos** (uno por módulo del árbol de directorios):
`config`, `exchanges`, `graph`, `evaluator`, `executor`, `risk`, `storage`, `telegram`, `infra`, `tests`

Ejemplos reales sobre el plan:
```
feat(exchanges): define ExchangeAdapter interface (ABC)
feat(graph): filter pairs by 24h volume and generate triangles without duplicates
feat(evaluator): compute net return using Decimal and BNB fee discount
feat(evaluator): discard triangles with stale book data
feat(risk): add max position size limit per trade
feat(risk): add daily loss auto-pause breaker
feat(executor): dispatch 3 FOK legs in parallel via asyncio.gather
feat(executor): reconcile inventory after partial leg failure
fix(storage): enable SQLite WAL mode to avoid lock contention
test(risk): simulate circuit breaker on repeated reconciliation incidents
docs: add risk management section to technical plan
chore(infra): multi-stage Dockerfile to reduce image size
```

**Commits atómicos:** un commit = un cambio lógico coherente. Si la descripción necesita "y" para explicarlo, probablemente son dos commits. Esto es lo que permite después usar `git bisect` sin sufrir, si en producción aparece un bug y no se sabe en qué commit se introdujo.

---

## 3. Tags y Milestones

Un tag por fase completada y validada (no por cada feature). Formato `vX.Y.Z-<hito>`:

```
v0.1.0-fase1-graph        # Fase 1 completa: filtro + generación de triángulos
v0.2.0-fase2-evaluator    # Fase 2 completa: WS + evaluación en tiempo real
v0.3.0-fase3-dryrun       # Fase 3 completa: dry run validado varios días
v0.4.0-fase4-telegram     # Fase 4 completa: control-plane operativo
v0.5.0-fase5-vps-dryrun   # Desplegado en VPS, aún en DRY_RUN
v1.0.0-live               # Primera vez con DRY_RUN=False y capital real
```

El salto a `v1.0.0` es deliberadamente significativo — marca el único momento del proyecto donde el riesgo cambia de "bug en el código" a "bug con dinero real perdido". Vale la pena que ese tag sea memorable y que el commit que lo acompaña incluya en el mensaje qué capital de prueba y qué límites de `risk.py` estaban activos en ese momento exacto (referencia rápida si después se necesita reconstruir el contexto).

---

## 4. Pull Requests (aunque sea el único dev)

Aunque no haya otro reviewer, abrir PR de `feature/*` → `develop` en vez de mergear directo tiene valor real acá:

- Obliga a revisar el diff completo antes de integrar (revisión asincrónica con la cabeza fría de "¿esto es lo que quería escribir?").
- Cada PR es la unidad natural para correr el checklist de tests de esa tarea antes de integrar.
- Queda un registro searchable de decisiones — se puede poner en la descripción del PR *por qué* se eligió, por ejemplo, ejecutar las 3 patas en paralelo y no secuencial, y ese razonamiento no se pierde en el historial de commits sueltos.

**Squash merge** de `feature/*` a `develop`: mantiene el historial de `develop` limpio (un commit resumen por feature), mientras que el detalle de commits atómicos queda preservado en la rama del PR si se necesita bucear.

`develop` → `main`: **merge commit** (no squash), para preservar la trazabilidad de qué conjunto de features compone cada release taggeada.

### 4.1 Título de la PR

Mismo formato que el commit (Conventional Commits), porque en squash merge el título de la PR termina siendo el mensaje del commit resultante en `develop`:

```
<type>(<scope>): <imperative description>
```

Ejemplos:
```
feat(executor): dispatch 3 FOK legs in parallel with reconciliation
feat(risk): add daily loss auto-pause breaker
fix(storage): enable SQLite WAL mode to avoid lock contention
```

Si la PR agrupa varias tareas relacionadas de un mismo bloque del checklist de fase, usar el tipo/scope dominante y listar el resto en la descripción — no forzar un título genérico tipo "Phase 3 changes".

### 4.2 Descripción de la PR (template)

Usar siempre esta estructura, incluso siendo el único revisor — es lo que da el registro de decisiones que después alimenta los ADRs (`plan_doc_DQ.md`, §3):

```markdown
## What it does
2-3 line summary of the change, in terms of the bot's behavior,
not a line-by-line implementation walkthrough.

## Why
The reasoning behind the decision, especially if there was an obvious
alternative that got discarded. If this warrants an ADR, reference it
here (or add the ADR in this same PR).

## How it was tested
- [ ] New/updated unit tests (which ones)
- [ ] DRY_RUN run (if applicable) — observed result
- [ ] Edge cases explicitly considered (e.g. what happens if leg 2
      fails and leg 3 already executed?)

## Risk / capital impact
Mark N/A if not applicable. If the change touches `risk.py`,
`executor.py`, capital limits, or the `DRY_RUN` flag, describe the
impact explicitly — this section is what saves you time later if you
need to audit an incident.

## Checklist
- [ ] Docstrings added/updated
- [ ] `docs/calibrations.md` updated (if a threshold was set or changed)
- [ ] No secrets or credentials in the diff
```

La sección **"Risk / capital impact"** es la única que no es boilerplate genérico de buenas prácticas — es específica de este proyecto, y la idea es que sea imposible mergear un cambio a `risk.py` o `executor.py` sin haber escrito, aunque sea en una línea, qué cambia en términos de plata.

---

## 5. Roadmap mapeado a Git, fase por fase

### Fase 0 — Fundamentos de entorno
```
chore/f0-docker-local-setup
chore/f0-vps-ssh-practice        (sin código de bot todavía, solo notas/scripts de práctica)
```
No genera tag de versión — es setup personal, no código del producto.

### Fase 1 — Análisis de Mercado y Filtro de Triángulos
```
feature/f1-project-scaffold          → estructura de carpetas, requirements.txt, .gitignore
feature/f1-pydantic-settings         → config/settings.py
feature/f1-exchange-adapter-interface → exchanges/base.py (ABC)
feature/f1-binance-adapter-readonly  → exchanges/binance_adapter.py (solo lectura)
feature/f1-graph-triangle-generation → core/graph.py
test/f1-graph-no-duplicates          → tests/test_graph.py
```
→ merge a `develop`, luego a `main`, tag `v0.1.0-fase1-graph`

### Fase 2 — WebSocket y Evaluación en Tiempo Real
```
feature/f2-binance-ws-bookticker     → binance_adapter.py completo con ccxt.pro
feature/f2-fees-bnb-discount         → exchanges/fees.py
feature/f2-evaluator-core            → core/evaluator.py, cálculo Decimal
feature/f2-evaluator-staleness-check → chequeo de datos viejos
test/f2-evaluator-known-spreads      → tests/test_evaluator.py
```
→ tag `v0.2.0-fase2-evaluator`

### Fase 3 — Simulación, Riesgo y Persistencia
```
feature/f3-sqlite-wal-models         → storage/database.py, models.py (incl. Incidents)
feature/f3-risk-capital-limits       → core/risk.py: tamaño máximo por operación
feature/f3-risk-daily-loss-breaker   → auto-pausa por pérdida diaria
feature/f3-executor-parallel-dryrun  → core/executor.py, ejecución paralela simulada
feature/f3-executor-reconciliation   → lógica de reconciliación ante fallo de pata
test/f3-reconciliation-simulated-fail → tests/test_reconciliation.py
```
Nota: antes de tagear, correr el dry run el período mínimo definido en el plan y documentar resultados (nuevo archivo `docs/dryrun_results_fase3.md` o similar, versionado también).
→ tag `v0.3.0-fase3-dryrun`

### Fase 4 — Telegram y Redis (control-plane)
```
chore/f4-redis-controlplane-only     → redis_client.py, SOLO TRADING_ENABLED/checkpoints
feature/f4-telegram-bot-commands     → /status, /kill, /resume, /pnl
feature/f4-telegram-incident-alerts  → alertas prioritarias de reconciliación
```
→ tag `v0.4.0-fase4-telegram`

### Fase 5 — Dockerización y Despliegue en VPS
```
chore/f5-dockerfile-multistage
chore/f5-docker-compose-bot-redis
chore/f5-vps-hardening               → notas/scripts: usuario no-root, firewall, fail2ban
chore/f5-api-key-ip-whitelist        → actualizar whitelist apuntando al VPS
```
→ tag `v0.5.0-fase5-vps-dryrun` (todavía con `DRY_RUN=True` en el VPS real)

**El paso a capital real no es una feature branch.** Es un cambio de configuración (`DRY_RUN=False`) documentado en un commit propio y explícito:
```
chore(infra): enable DRY_RUN=False with initial test capital

Active limits in core/risk.py at the time of this change:
- max position size per trade: <value>
- daily loss limit: <value>
- max concurrent triangles: <value>
```
→ tag `v1.0.0-live`

### Fase 6 — Multi-exchange (futuro)
```
feature/f6-bybit-adapter             → exchanges/bybit_adapter.py implementando ExchangeAdapter
test/f6-bybit-adapter-interface-compliance
```
→ tag `v1.1.0-multiexchange` (o el siguiente minor, según semver — es una feature aditiva, no rompe la interfaz existente si `ExchangeAdapter` se respetó desde la Fase 1)

---

## 6. Extras recomendados

- **`CHANGELOG.md` autogenerado**: con Conventional Commits, herramientas como `git-cliff` o `commitizen` generan el changelog automáticamente a partir del historial — sin mantenimiento manual.
- **Nunca commitear `.env`**: solo `.env.example` versionado, con placeholders. Doble chequeo en `.gitignore` desde el primer commit de la Fase 1, antes de que exista ninguna credencial real en el filesystem del repo.
- **Pre-commit hook** para evitar commitear secretos por accidente (`detect-secrets` o similar) — barato de configurar en Fase 1 y elimina una categoría entera de errores humanos después.
