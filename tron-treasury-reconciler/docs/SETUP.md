# Guia de Configuracion y Despliegue

Instrucciones paso a paso para configurar **Tron Treasury Reconciler** en entorno local, testnet o VPS.

---

## 1. Requisitos

- **Python 3.10 o superior** (verificado en Python 3.14).
- **PostgreSQL** (local, Neon, Supabase o Docker) con el esquema de [sql/schema.sql](../sql/schema.sql) aplicado.
- Cuenta o API key en [TronGrid](https://www.trongrid.io/) (recomendada para evitar limites de tasa).
- API key de [ChangeNOW](https://changenow.io/) (requerida unicamente cuando se ejecutan barridos reales hacia Arbitrum).

---

## 2. Instalacion

```bash
python -m venv .venv
# Windows PowerShell
.venv\Scripts\Activate.ps1
# Linux / macOS
source .venv/bin/activate

pip install --upgrade pip
pip install -r requirements-dev.txt
```

Crea tu archivo `.env` a partir de la plantilla:

```bash
# Windows PowerShell
Copy-Item .env.example .env
# Linux / macOS
cp .env.example .env
```

---

## 3. Configuracion de variables (`.env`)

| Variable | Por defecto | Descripcion |
| :--- | :--- | :--- |
| `TRON_NETWORK` | `nile` | Red destino: `nile`, `shasta` o `mainnet`. |
| `DRY_RUN` | `true` | Si es `true`, simula swaps y transferencias sin firmar ni transmitir a la red. |
| `TICK_API_SECRET` | *(vacia)* | Secreto compartido para autorizar `POST /tick`. Obligatorio para ejecutar ciclos. |
| `TRON_PUBLIC_ADDRESS` | *(vacia)* | Direccion publica Tron (`T...`) receptora de depositos. |
| `TRON_PRIVATE_KEY` | *(vacia)* | Clave privada hex de 64 caracteres (solo requerida cuando `DRY_RUN=false`). |
| `TRONGRID_API_KEY` | *(vacia)* | Header `TRON-PRO-API-KEY` para consultas a TronGrid. |
| `TRONGRID_BASE_URL` | *(segun red)* | Opcional; sobreescribe la URL de TronGrid del preset de red. |
| `TRON_USDT_CONTRACT` | *(segun red)* | Opcional; sobreescribe el contrato TRC-20 de USDT. |
| `TRON_WTRX_CONTRACT` | *(segun red)* | Opcional; necesario si ejecutas auto-swap SunSwap en testnet. |
| `TRON_SUNSWAP_ROUTER` | *(segun red)* | Opcional; router SunSwap para swap USDT -> TRX en testnet. |
| `ARBITRUM_VAULT_ADDRESS` | *(vacia)* | Direccion destino `0x...` en Arbitrum para barridos ChangeNOW. |
| `CHANGENOW_API_KEY` | *(vacia)* | API key v2 de ChangeNOW. |
| `DATABASE_URL` | *(vacia)* | Cadena de conexion PostgreSQL (`postgresql://user:pass@host/db?sslmode=require`). |
| `MIN_TRX_GAS` | `35.0` | Reserva minima de TRX antes de activar el auto-swap de gas. |
| `USDT_PARA_SWAP_GAS` | `10.0` | Cantidad de USDT a convertir a TRX cuando falta gas. |
| `MONTO_VERIFICACION_USDT` | `10.0` | Monto minimo requerido para activar `cuenta_verificada` en `users`. |
| `MAX_SLIPPAGE_BPS` | `150` | Tolerancia de slippage en SunSwap en puntos basicos (`150` = 1.50%, rango `10`–`500`). |
| `LOG_LEVEL` | `INFO` | Nivel de logging (`DEBUG`, `INFO`, `WARNING`, `ERROR`). |

---

## 4. Inicializar la base de datos

Aplica el esquema de referencia en tu instancia PostgreSQL:

```bash
psql "$DATABASE_URL" -f sql/schema.sql
```

Si ya cuentas con las tablas `users` y `depositos` en produccion, puedes anadir unicamente las tablas opcionales `reconciliation_audit`, `treasury_sweeps` y los indices unicos de `tx_hash` definidos en [sql/schema.sql](../sql/schema.sql).

---

## 5. Pruebas automatizadas (offline)

Las pruebas simulan TronGrid, PostgreSQL y ChangeNOW sin necesidad de fondos ni credenciales reales:

```bash
python -m pytest -v
```

---

## 6. Ejecucion en local / Testnet (`Nile`)

1. Configura `TRON_NETWORK=nile`, `DRY_RUN=true`, un `TICK_API_SECRET` aleatorio (por ejemplo generado con `python -c "import secrets; print(secrets.token_urlsafe(32))"`), `TRON_PUBLIC_ADDRESS` y `DATABASE_URL`.
2. Inicia el servidor:

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

3. Verifica estado de solo lectura:

```bash
curl http://127.0.0.1:8000/health
```

4. Dispara un ciclo de conciliacion autenticado:

```bash
curl -X POST http://127.0.0.1:8000/tick \
  -H "X-Tick-Secret: TU_SECRETO"
```

Una vez validado el comportamiento en `DRY_RUN=true` sobre `nile`, puedes desactivar `DRY_RUN=false` con una wallet de pruebas y fondos de faucet antes de pasar a `mainnet`.

---

## 7. Operacion en VPS (Cron o Systemd)

En produccion, enlaza FastAPI a `127.0.0.1` detras de un proxy inverso con TLS (Nginx/Caddy) o restringe el acceso localmente si solo se dispara desde cron en la misma maquina:

```cron
*/2 * * * * curl -sf -X POST http://127.0.0.1:8000/tick -H "X-Tick-Secret: TU_SECRETO" >/dev/null 2>&1
```

---

## 8. Subir a un repositorio publico limpio

```bash
git init
git add .
git status   # Confirma que .env NO aparece en el staging y .env.example SI aparece
git commit -m "feat: modular tron trc20 reconciler and treasury service"
```
