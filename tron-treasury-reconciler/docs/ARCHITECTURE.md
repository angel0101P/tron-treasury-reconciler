# Arquitectura y Modelo de Seguridad

Este documento detalla el funcionamiento interno de **Tron Treasury Reconciler**, las decisiones de seguridad aplicadas y las limitaciones operativas conocidas.

---

## 1. Flujo de ejecucion (`POST /tick`)

El servicio sigue un modelo *pull* determinista disparado bajo demanda o mediante un programador externo (cron / systemd timer):

1. **Autenticacion y control de concurrencia (`app/main.py`):**
   - `require_tick_auth` exige que `TICK_API_SECRET` este definido (si falta, responde `503` para no quedar expuesto por omision). Valida `X-Tick-Secret` o `Authorization: Bearer <token>` con `hmac.compare_digest`.
   - `cycle_lock` impide ejecuciones paralelas simultaneas; si un ciclo sigue en curso, retorna `409 Conflict`.
2. **Lectura directa en cadena (`app/tron_client.py`):**
   - Consulta `GET /v1/accounts/{TRON_PUBLIC_ADDRESS}/transactions/trc20` en TronGrid con `only_to=true` y `only_confirmed=true`, filtrando por `contract_address`.
   - El servidor nunca acepta callbacks sin verificar ni montos enviados desde el cliente.
3. **Validacion y conciliacion SQL (`app/reconciler.py`):**
   - Por cada deposito en estado `'PENDIENTE'` o usuario con `cuenta_verificada = FALSE` que tenga `tx_hash`, contrasta el hash normalizado contra las transacciones confirmadas.
   - `validate_trc20_transfer` comprueba:
     - `tx["to"] == settings.tron_public_address`
     - `token_info["address"] == settings.usdt_contract`
     - `tx["type"] == "Transfer"`
     - `int(tx["value"]) >= expected_micro` en enteros exactos (1 USDT = `1_000_000` micro-unidades).
   - Un conjunto `used_hashes` impide reclamar el mismo hash dos veces dentro de la misma corrida, complementado por indices unicos parciales en PostgreSQL (`sql/schema.sql`).
   - Cada evento aprobado o rechazado se registra en `reconciliation_audit` mediante `SAVEPOINT` para preservar la integridad de la transaccion principal.
4. **Gestion de gas y barrido cross-chain (`app/treasury.py`):**
   - Verifica si el saldo en TRX supera `MIN_TRX_GAS`. De lo contrario, si hay saldo USDT suficiente y `DRY_RUN=false`, consulta `getAmountsOut` en SunSwap, calcula `min_trx_out` segun `MAX_SLIPPAGE_BPS`, aprueba **exactamente** la cantidad de entrada y ejecuta `swapExactTokensForETH`.
   - Si el saldo USDT supera el minimo de ChangeNOW v2 (`get_min_amount`), cuantifica a 2 decimales hacia abajo (`ROUND_DOWN`) usando `Decimal`, solicita una orden en ChangeNOW (`create_exchange_order`) verificando que la `payinAddress` devuelta supere `is_valid_tron_address` (Base58Check) y registra el barrido en `treasury_sweeps` antes y despues de transmitir `send_usdt`.

---

## 2. Matriz de amenazas y mitigaciones

| Amenaza / Riesgo | Estado previo | Mitigacion implementada |
| :--- | :--- | :--- |
| Disparo publico de `/tick` por terceros | `GET/POST /tick` abierto sin auth | Solo `POST /tick`, secreto obligatorio *fail-closed* (`503`/`401`) |
| Ataque sandwich / MEV en SunSwap | `amountOutMin = 1` sin cotizacion | Consulta `getAmountsOut` + `calculate_min_out` con `MAX_SLIPPAGE_BPS` |
| Exposicion por `approve` ilimitado al DEX | `approve(router, 10**15)` | `approve(router, amount_in_micro)` exacto por operacion |
| Suplantacion de token TRC-20 o destinatario | Solo indexaba por `transaction_id` | Validacion explicita de `to`, `token_info.address` y `type` |
| Errores de redondeo financiero | Uso de `float` al comparar montos | Conversion y comparacion en enteros `int` (`to_micro_usdt`) y `Decimal` |
| Reutilizacion de un mismo `tx_hash` | Sin control en codigo ni esquema SQL | Set `used_hashes` + indices unicos y funciones `FOR UPDATE` en SQL |
| Direccion corrupta devuelta por API externa | Sin validacion de `payinAddress` | Validacion Base58Check (`0x41`, 34 chars, checksum) antes de enviar |
| Filtracion de detalles internos en HTTP 500 | Devolvia `{"detail": str(e)}` | Logging interno con `logger.exception` y respuesta generica al cliente |
| Ejecucion accidental en local o pruebas | Contratos mainnet fijos y firma al importar | Inicializacion diferida de clave privada, `DRY_RUN=true` y presets `nile`/`shasta` |

---

## 3. Limitaciones operativas conocidas

- **Custodia de clave caliente (`TRON_PRIVATE_KEY`):** al requerir firma autonoma de transacciones desde un VPS, la clave privada reside en variables de entorno del servidor. En despliegues productivos debe restringirse el acceso al host, rotar la wallet periodicamente y mantener saldos bajos mediante barridos frecuentes.
- **Paginacion de TronGrid (`limit=100`):** si la wallet recibe mas de 100 transferencias TRC-20 confirmadas antes de liquidar los depositos pendientes antiguos, las mas antiguas podrian quedar fuera de la primera pagina. Para alto volumen se recomienda agregar paginacion por `fingerprint` o ventana `min_timestamp`.
- **Riesgo de contraparte en puente cross-chain:** ChangeNOW v2 opera como servicio externo custodial durante el intercambio USDT (TRC-20) -> ETH (Arbitrum). La tabla `treasury_sweeps` guarda `order_id`, `payin_address` y `tron_txid` para auditoria y conciliacion manual en caso de incidencia.
- **Testnets y liquidez DEX:** los presets de `nile` y `shasta` permiten probar consultas de TronGrid, conciliacion en PostgreSQL y transferencias TRC-20; para probar el auto-swap de SunSwap en testnet es necesario configurar `TRON_WTRX_CONTRACT` y `TRON_SUNSWAP_ROUTER` hacia un pool con liquidez activa en esa testnet.
