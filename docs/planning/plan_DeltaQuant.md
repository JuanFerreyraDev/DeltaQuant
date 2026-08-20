# Plan de Proyecto: DeltaQuant (Bot de Arbitraje Triangular Automatizado (Binance → Multi-CEX))

## 0. Contexto y expectativas honestas

Antes de la arquitectura, esto hay que decirlo explícitamente porque el objetivo declarado es **rentabilidad real desde el inicio**:

En exchanges top-tier como Binance, el arbitraje triangular puro está dominado por market makers profesionales con:
- Colocación física cerca del matching engine (latencia de microsegundos vs. los ~50-150ms que vas a tener desde un VPS en la nube pública).
- Motores en C++/Rust, no Python (aunque con `asyncio`+`uvloop` bien hecho, Python puede ser "suficientemente rápido" para *algunas* oportunidades, no para todas).
- Tarifas maker/taker mínimas (VIP9, tokens propios) que vos no vas a tener al principio.

**Esto no significa que el proyecto no valga la pena o no pueda ser rentable**, pero condiciona decisiones de diseño:

- **No vas a competir por las oportunidades de <0.05% de spread que cierran en 10ms.** Vas a competir por ineficiencias más lentas: mercados con volumen medio, momentos de alta volatilidad/noticias donde el book se desincroniza brevemente, o pares menos vigilados.
- La rentabilidad real depende tanto o más de la **gestión de comisiones** (BNB fee discount, VIP tier) y del **filtrado inteligente de oportunidades** que de la latencia pura.
- El diseño multi-exchange futuro es valioso justamente por esto: monitorear varios CEX en simultáneo te da más superficie de oportunidades que pelear por latencia en uno solo.

Con esto claro, el plan está diseñado para maximizar tus chances reales, no para prometer algo que la arquitectura no puede entregar.

---

## 1. Visión General del Sistema

Bot de trading algorítmico que detecta y ejecuta arbitraje triangular intra-CEX (comenzando en Binance, con arquitectura preparada para agregar exchanges adicionales sin reescritura). Opera 24/7 en un VPS remoto, con control y alertas vía Telegram desde PC/celular.

### Principios de diseño
- **Sin riesgo de inventario planeado:** capital base en una sola moneda (USDT), órdenes FOK/IOC para las 3 patas. *Pero* con lógica explícita de reconciliación para el caso (raro pero real) en que una pata falle tras haberse ejecutado otra.
- **Exchange-agnostic desde el día 1 a nivel de interfaz**, aunque la implementación inicial y el capital real solo operen en Binance. Esto evita una reescritura cuando agregues el segundo exchange.
- **Optimización de comisiones como primera palanca de rentabilidad**, no una idea secundaria.
- **Redis fuera del hot path.** Se usa para control-plane (kill switch, estado persistente entre reinicios) y telemetría, nunca en el ciclo de evaluación tick-a-tick, que vive 100% en memoria del proceso.

---

## 2. Stack Tecnológico

| Componente | Tecnología | Justificación |
|---|---|---|
| Lenguaje core | Python 3.11+ | `asyncio` maduro, tipado, ecosistema |
| Librería CEX | `ccxt.pro` | WebSocket unificado; ver §3.1 sobre límites |
| Event loop | `uvloop` | Reemplazo de libuv para el loop, reduce overhead |
| Control-plane / cache | `Redis` (`redis-py` async) | Kill switch, estado entre reinicios, NO hot path |
| Persistencia | `SQLite` + `SQLAlchemy` async, modo WAL | Historial de trades y métricas |
| Panel de control | `python-telegram-bot` v20+ | Comandos y alertas |
| Config | `pydantic-settings` | Validación estricta de `.env` |
| Precisión | `decimal` | Evita errores de redondeo binario |
| Logging | `loguru` | Logs estructurados con rotación |
| Despliegue | `Docker` + `Docker Compose` | Aislamiento y auto-recuperación |
| Monitoreo (nuevo) | `Prometheus` + `Grafana` (opcional, fase tardía) | Métricas de latencia y fill rate — más preciso que revisar logs de Telegram a mano |

### 3.1 Nota sobre `ccxt.pro`
Es la elección correcta para arrancar por velocidad de desarrollo, pero agrega overhead de abstracción. Si en producción medís que la latencia de parseo de mensajes WS es un cuello de botella real (lo vas a saber por las métricas de §7), la salida es reemplazar el cliente WS de Binance por una integración directa con `websockets` + parseo manual del stream `bookTicker`, manteniendo el resto de la arquitectura intacta gracias a la capa de abstracción del §3.2.

### 3.2 Diseño para multi-exchange futuro
Se define una interfaz `ExchangeAdapter` (ABC) con métodos async: `subscribe_book_ticker`, `place_fok_order`, `get_balance`, `get_trading_fees`. La implementación inicial `BinanceAdapter` es la única con capital real. Agregar un segundo exchange (ej. Bybit) más adelante significa escribir un nuevo adapter, no tocar `evaluator.py` ni `executor.py`. El grafo de triángulos y el motor de evaluación son agnósticos al origen de los precios.

---

## 3. Arquitectura y Flujo de Datos

```text
[ Binance WS/REST ]      [ Adapter Exchange 2 (futuro) ]
        │                          │
        ▼                          ▼
┌─────────────────────────────────────────────┐
│         CAPA 1: TRADING ENGINE (RAM)         │
│  graph.py      → triángulos por exchange     │
│  evaluator.py  → evaluación matemática       │
│  executor.py   → despacho FOK + reconcile    │
│  risk.py       → límites de capital/pérdida  │  ← nuevo
└───────────────────────┬───────────────────────┘
                         │ (solo eventos de control/telemetría,
                         │  NUNCA precios tick-a-tick)
                         ▼
┌─────────────────────────────────────────────┐
│      CAPA 2: CONTROL-PLANE E INFRA           │
│  Redis  → TRADING_ENABLED, checkpoints       │
│  SQLite → historial de trades y métricas     │
└───────────────────────┬───────────────────────┘
                         ▼
┌─────────────────────────────────────────────┐
│         CAPA 3: INTERFAZ Y CONTROL           │
│  Telegram (alertas, /kill, /status)          │
│  Prometheus/Grafana (opcional, fase tardía)  │
└─────────────────────────────────────────────┘
```

### Flujo de evaluación
1. **Filtro periódico de pares** (cada N horas vía REST): volumen 24h > umbral configurable por exchange.
2. **Generación de triángulos** en memoria a partir de los pares filtrados.
3. **Suscripción WS** a `bookTicker` de los pares involucrados, exclusivamente.
4. **Cálculo en cada tick**, 100% en memoria, sin I/O:
   ```
   retorno_neto = (tasa1 × tasa2 × tasa3) − comisiones_totales(con descuento BNB si aplica)
   si retorno_neto > 1.0 + margen_seguridad: candidato de ejecución
   ```
5. **Chequeo de staleness**: si el precio de cualquiera de los 3 pares no se actualizó en los últimos X ms, se descarta el triángulo aunque matemáticamente parezca rentable (protección contra datos viejos).
6. **Chequeo de risk.py**: ¿hay presupuesto disponible según límites de capital/pérdida diaria? ¿no se superó el máximo de triángulos concurrentes?
7. **Ejecución**: las 3 órdenes FOK se despachan **en paralelo** (no secuencial) vía `asyncio.gather`, para minimizar la ventana de movimiento de precio entre patas.
8. **Reconciliación post-ejecución**: se verifica el resultado real de las 3 órdenes. Si alguna falló tras que otra se ejecutó, se dispara una **liquidación de emergencia** del inventario no planeado al mejor precio de mercado disponible, se loguea como incidente y se alerta por Telegram inmediatamente.

---

## 4. Estructura de Directorios

```text
DeltaQuant/
│
├── docs/
│   ├── planning/
│   ├── fases/
│   ├── adr/
│   └── runbooks/
│   
├── config/
│   ├── settings.py             # Pydantic: credenciales, umbrales, límites de riesgo
│   └── logging_config.py
│
├── exchanges/                   # NUEVO: capa de abstracción multi-exchange
│   ├── base.py                  # ABC ExchangeAdapter
│   ├── binance_adapter.py       # Implementación real, único con capital vivo
│   └── fees.py                  # Cálculo de comisiones por exchange/tier/descuento
│
├── core/
│   ├── graph.py                 # Filtro por volumen + generación de triángulos
│   ├── evaluator.py             # Evaluación matemática en RAM + staleness check
│   ├── executor.py              # Despacho paralelo FOK/IOC
│   └── risk.py                  # NUEVO: límites de capital, pérdida diaria, concurrencia
│
├── interfaces/
│   ├── telegram_bot.py          # /start, /kill, /resume, /status, /pnl
│   └── notifier.py
│
├── storage/
│   ├── database.py              # SQLite async, WAL mode
│   ├── models.py                # Trades, Metrics, Incidents (reconciliación)
│   └── redis_client.py          # Solo control-plane
│
├── tests/
│   ├── test_evaluator.py
│   ├── test_graph.py
│   ├── test_executor.py
│   ├── test_risk.py             # NUEVO
│   └── test_reconciliation.py   # NUEVO: simula fallo de pata 2/3
│
├── logs/
├── .env.example
├── .gitignore
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── main.py
```

---

## 5. Gestión de Riesgo y Capital (`risk.py`)

Módulo explícito, no implícito en el flag `TRADING_ENABLED`:

- **Tamaño máximo por operación**: % fijo o monto fijo del capital total configurable en `.env`.
- **Límite de pérdida diaria**: si el PnL acumulado del día cae por debajo de un umbral, el bot se auto-pausa y alerta (no espera a que lo pares manualmente).
- **Máximo de triángulos concurrentes**: evita sobreexposición si varias oportunidades disparan al mismo tiempo.
- **Circuit breaker por incidentes de reconciliación**: si ocurren más de N liquidaciones de emergencia en una ventana de tiempo, el bot se pausa solo — es señal de que algo estructural está mal (latencia, símbolo problemático, bug).

---

## 6. Optimización de Comisiones (clave para rentabilidad real)

Dado que el objetivo es rentabilidad desde el inicio, esto es tan importante como la latencia:

- **Descuento BNB**: activar el pago de comisiones en BNB en Binance (25% de descuento estándar). El módulo `fees.py` debe calcular el retorno neto usando la tasa efectiva real, no la nominal.
- **VIP tier tracking**: el volumen de trading propio puede escalar el tier con el tiempo; `fees.py` debe permitir actualizar la tasa sin tocar el resto del código.
- **Filtro de pares por spread mínimo viable**: descartar triángulos donde el margen teórico máximo posible (considerando spread bid/ask típico del par) ya es menor que las comisiones totales — no vale la pena ni evaluarlos en tiempo real.

---

## 7. Seguridad, Reconciliación y Observabilidad

- **API Keys**: solo lectura + trading spot, **sin retiros**, restringidas a la IP del VPS.
- **Decimal en todo el pipeline numérico.**
- **Dry Run** (`DRY_RUN=True`): simula contra precios reales sin enviar órdenes.
- **Kill switch** (`/kill` en Telegram → `TRADING_ENABLED=False` en Redis).
- **Reconciliación de inventario** (§3, paso 8): tabla `Incidents` en SQLite, alerta inmediata.
- **Staleness detection**: timestamp de cada tick de book, descarte si supera umbral.
- **Métricas mínimas a trackear desde el día 1** (para saber si el proyecto es viable, no solo si "funciona"): latencia tick→decisión, latencia decisión→orden enviada, fill rate real de FOK (cuántas veces se cumple vs. se cancela), PnL neto después de comisiones.

---

## 8. Hoja de Ruta

### Fase 0: Fundamentos de entorno (nueva — dado que no hay experiencia previa en VPS/Docker)
- [ ] Instalar Docker Desktop localmente y correr el bot en modo `DRY_RUN` en tu propia PC primero — **no saltar directo al VPS**.
- [ ] Familiarizarte con comandos básicos de Docker (`build`, `up`, `logs`, `exec`) sobre este mismo proyecto antes de tocar infraestructura remota.
- [ ] Crear cuenta en un proveedor de VPS (recomendado para empezar: uno con datacenter en la misma región que el exchange — para Binance, considerar su región de infraestructura principal) y practicar acceso SSH básico con una instancia mínima, sin el bot todavía.

### Fase 1: Análisis de Mercado y Filtro de Triángulos
- [ ] Repositorio Git + `config/settings.py` con Pydantic.
- [ ] `exchanges/base.py` (interfaz ABC) + `exchanges/binance_adapter.py` mínimo (solo lectura de mercado).
- [ ] `core/graph.py`: volumen 24h vía REST, generación de triángulos sin duplicados.

### Fase 2: WebSocket y Evaluación en Tiempo Real
- [ ] `binance_adapter.py` completo con `ccxt.pro`, suscripción `bookTicker`.
- [ ] `core/evaluator.py` con cálculo `Decimal` + staleness check.
- [ ] `exchanges/fees.py` con tasa BNB discount aplicada al cálculo.
- [ ] Log de spreads positivos detectados (sin ejecutar).

### Fase 3: Simulación, Riesgo y Persistencia
- [ ] `storage/database.py` con SQLite WAL + `models.py` (incluye tabla `Incidents`).
- [ ] `core/risk.py`: límites de capital, pérdida diaria, concurrencia.
- [ ] `core/executor.py` en modo Dry Run: ejecución paralela simulada + lógica de reconciliación simulando fallos de pata.
- [ ] Correr en `DRY_RUN` varios días, revisar métricas de fill rate teórico y PnL neto de comisiones antes de avanzar.

### Fase 4: Telegram y Redis (control-plane)
- [ ] Redis local, solo para `TRADING_ENABLED` y checkpoints.
- [ ] `interfaces/telegram_bot.py`: `/status`, `/kill`, `/resume`, `/pnl`.
- [ ] Alertas de incidentes de reconciliación con prioridad alta.

### Fase 5: Dockerización y Despliegue en VPS (guía detallada, paso a paso)
- [ ] `Dockerfile` + `docker-compose.yml` (bot + Redis).
- [ ] Contratar VPS Linux en región cercana a la infraestructura de Binance.
- [ ] Guía paso a paso de hardening básico: usuario no-root, firewall (solo puertos necesarios), fail2ban, actualización de IP whitelisting en las API keys de Binance apuntando al VPS.
- [ ] Deploy con `DRY_RUN=True` primero en el VPS real durante varios días — la latencia de red real cambia respecto a tu PC local, hay que remedir métricas ahí.
- [ ] Solo entonces, `DRY_RUN=False` con capital mínimo de prueba (definir monto explícito antes de arrancar, no "lo que sobre").

### Fase 6: Multi-exchange (futuro, post-validación en Binance)
- [ ] Solo abordar una vez que Binance esté validado con datos reales de rentabilidad neta positiva sostenida.
- [ ] Nuevo adapter (ej. `bybit_adapter.py`) implementando la misma interfaz `ExchangeAdapter`.
- [ ] Evaluar si conviene arbitraje triangular intra-exchange en el segundo CEX, o si el valor real está en detectar divergencias de precio *entre* exchanges (proyecto distinto, con riesgo de transferencia — decisión a tomar con datos, no de antemano).

---

## 9. Consideraciones Adicionales

### 9.1 El problema del backtesting en arbitraje triangular
No es backtesteable de forma seria con datos OHLCV convencionales — hace falta granularidad de tick/order book, y `bookTicker` solo da el top of book (mejor bid/ask), no profundidad. Una oportunidad que luce rentable en el top puede evaporarse al intentar ejecutar un tamaño que agota esa liquidez.
- Suscribirse también a un stream de profundidad parcial (`depth5`/`depth10`) para estimar volumen realmente ejecutable, no solo precio.
- El "backtest" real en la práctica es el `DRY_RUN` corriendo en vivo durante semanas — no hay atajo con datos históricos gratuitos de calidad suficiente para esta estrategia.

### 9.2 Riesgo de sobreajuste del margen de seguridad
El parámetro `margen_seguridad` (§3, paso 4) es fácil de sobreajustar si se calibra mirando qué "hubiera funcionado" en los propios logs de dry run a posteriori. Debe fijarse con una lógica explícita (costos + buffer de slippage estimado) **antes** de mirar los resultados del dry run, no ajustarse después de ver qué spread hubiera dado ganancia — mismo principio que un protocolo pre-registrado.

### 9.3 Rate limits (weight) de Binance
Binance limita por "weight" consumido por IP y por API key, no solo por cantidad de requests. Evaluar muchos triángulos y refrescar volumen 24h con frecuencia puede agotar ese límite sin aviso previo y derivar en un ban temporal. Loguear el weight consumido desde el día 1 (no reactivamente después de un ban) — candidato natural para agregar a `core/risk.py` o a un módulo de rate-limit tracking dedicado.

### 9.4 Sizing dinámico por oportunidad
El tamaño de la operación no debería ser un cap fijo global. Una oportunidad con spread chico solo es rentable con tamaño pequeño (el book se agota rápido y el slippage come el margen). `risk.py` debería calcular el tamaño máximo *por oportunidad* en función de la profundidad disponible (ver §9.1 sobre streams de profundidad), no solo un límite global de capital.

### 9.5 Seguridad práctica adicional
- Activar 2FA en la cuenta de Binance (más allá de los permisos de la API key).
- Considerar whitelist de direcciones de retiro a nivel cuenta como defensa en profundidad, incluso si la API key ya no tiene permiso de retiro — cubre el caso de que se comprometa la sesión web, no solo la API key.

### 9.6 Perspectiva a futuro: arbitraje de funding rate
Una vez validado y madurado el loop completo en spot, la extensión natural con menos competencia que el arbitraje triangular puro es el **arbitraje de funding rate entre spot y futuros dentro del mismo exchange** (comprar spot + short en perpetuo cuando el funding es favorable). Reutiliza gran parte de la infraestructura ya construida (evaluator, executor, risk, telegram), y las ineficiencias que explota suelen durar más que milisegundos porque dependen de sentimiento de mercado y no de latencia pura. Es un candidato razonable para un siguiente proyecto una vez que este esté maduro y validado con datos reales — no algo a incorporar ahora.

---

## 10. Qué NO está en el alcance de este plan (a propósito)

- Arbitraje inter-exchange (requiere transferencias, custodia en múltiples plataformas, riesgo de red — es un proyecto con perfil de riesgo distinto).
- Colocación física / infraestructura de latencia ultra-baja (fuera de alcance para un proyecto individual en etapa inicial).
- Trading con apalancamiento o futuros — este plan asume spot únicamente, consistente con "sin riesgo de inventario".
