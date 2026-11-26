# Tron Treasury Reconciler

Servicio backend en Python y FastAPI para **conciliacion on-chain de depositos USDT (TRC-20)** y **gestion automatizada de tesoreria cross-chain (Tron -> Arbitrum)**.

Consulta transferencias confirmadas directamente servidor a servidor contra la API REST de TronGrid, valida destinatario, contrato y monto exacto en micro-unidades contra registros pendientes en PostgreSQL, gestiona la reserva de gas (TRX) mediante SunSwap con proteccion de slippage y ejecuta barridos hacia una boveda en Arbitrum a traves de ChangeNOW v2.

---

## Arquitectura del ciclo

```
  Cron / Scheduler privado
          │  POST /tick  (X-Tick-Secret / Bearer)
          ▼
   FastAPI (app/main.py) ──► Lock no bloqueante (HTTP 409 si ocupado)
          │
          ├─► 1. Consulta S2S (app/tron_client.py) ◄── TronGrid REST (only_confirmed=true)
          │
          ├─► 2. Conciliacion (app/reconciler.py)  ◄──► PostgreSQL (sql/schema.sql)
          │      • Verifica to, contract_address, type='Transfer' y micro-USDT exactos
          │      • Bloquea reutilizacion de tx_hash en el ciclo e indices UNIQUE SQL
          │      • Registra auditoria en reconciliation_audit
          │
          └─► 3. Tesoreria (app/treasury.py)
                 • Auto-swap USDT -> TRX en SunSwap (getAmountsOut + slippage + approve exacto)
                 • Orden ChangeNOW v2 (app/changenow.py) validando payinAddress Tron Base58Check
                 • Registro previo y posterior en treasury_sweeps
```

## Mejoras de seguridad y diseno implementadas

- **Endpoint operativo protegido (`POST /tick`):** eliminada la exposicion por `GET` y protegida la ejecucion mediante `TICK_API_SECRET` con comparacion en tiempo constante (`hmac.compare_digest`) y comportamiento *fail-closed* (`503` si no hay secreto configurado).
- **Validacion estricta de transferencias TRC-20:** verifica en servidor que el destinatario coincida con `TRON_PUBLIC_ADDRESS`, que el contrato pertenezca al USDT configurado y que el evento sea `Transfer`, comparando montos con aritmetica entera (`int` micro-USDT / `Decimal`) en lugar de `float`.
- **Anti-reutilizacion de `tx_hash`:** deduplicacion en memoria por ciclo combinada con indices unicos parciales y funciones PL/pgSQL con `FOR UPDATE` en PostgreSQL.
- **Proteccion contra MEV/sandwich y aprobaciones excesivas en DEX:** `ensure_trx_gas` consulta `getAmountsOut`, calcula `amountOutMin` segun `MAX_SLIPPAGE_BPS` (rechazando cotizaciones nulas o <= 1 SUN) y aprueba unicamente el monto exacto a intercambiar.
- **Validacion de direcciones antes de firmar:** verificacion de checksum Base58Check para direcciones Tron (`T...`) y formato hex para la boveda EVM (`0x...`) antes de crear ordenes o transmitir fondos.
- **Modo seguro por defecto (`DRY_RUN=true`) y soporte de testnets:** permite probar integraciones en red `nile`, `shasta` o en local sin firmar ni transmitir transacciones.
- **Manejo limpio de errores:** las excepciones no controladas se registran internamente sin filtrar trazas ni cadenas de conexion al cliente HTTP.

## Estructura del repositorio

```
├── app/
│   ├── __init__.py
│   ├── config.py          # Presets de red, validadores Base58Check/EVM y aritmetica Decimal
│   ├── tron_client.py     # Cliente TronGrid REST, balances, SunSwap con slippage y transferencias
│   ├── changenow.py       # Cliente ChangeNOW v2 con validacion estricta de respuestas
│   ├── reconciler.py      # Validacion TRC-20, conciliacion SQL, auditoria y tracking de sweeps
│   ├── treasury.py        # Logica de barrido cross-chain con modo DRY_RUN
│   └── main.py            # API FastAPI, lifespan, autenticacion /tick y control de concurrencia
├── docs/
│   ├── ARCHITECTURE.md    # Flujo detallado, modelo de amenazas y limitaciones operativas
│   └── SETUP.md           # Guia de instalacion, variables .env, SQL, testnet y ejecucion
├── sql/
│   └── schema.sql         # Esquema de referencia PostgreSQL, indices y funciones idempotentes
├── tests/                 # Suite de pruebas unitarias y de API completamente offline
├── .env.example           # Plantilla de configuracion sin credenciales
├── .gitignore
├── LICENSE
├── requirements.txt
└── requirements-dev.txt
```

## Inicio rapido

```bash
python -m venv .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1
# Linux / macOS:      source .venv/bin/activate

pip install -r requirements-dev.txt
cp .env.example .env
```

Ejecuta las pruebas offline para validar el entorno:

```bash
python -m pytest -v
```

Levanta el servidor local (en modo `DRY_RUN=true` y red `nile` por defecto):

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Consulta la guia completa en [docs/SETUP.md](docs/SETUP.md) y el detalle de seguridad en [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
