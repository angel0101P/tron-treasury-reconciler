-- Esquema de referencia para PostgreSQL (Neon / Supabase / local)
-- Incluye tablas transaccionales, indices anti-reutilizacion de tx_hash,
-- auditoria de conciliacion, trazabilidad de barridos y funciones idempotentes.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username TEXT NOT NULL UNIQUE,
    saldo_usdt NUMERIC(18, 6) NOT NULL DEFAULT 0,
    cuenta_verificada BOOLEAN NOT NULL DEFAULT FALSE,
    metodo_verificacion TEXT,
    tx_hash_verificacion TEXT,
    verificada_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS depositos (
    id BIGSERIAL PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    monto_usdt NUMERIC(18, 6) NOT NULL CHECK (monto_usdt > 0),
    estado TEXT NOT NULL DEFAULT 'PENDIENTE' CHECK (estado IN ('PENDIENTE', 'APROBADO', 'RECHAZADO')),
    tx_hash_tron TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    aprobado_at TIMESTAMPTZ
);

-- Impide reutilizar el mismo tx_hash entre multiples depositos o verificaciones.
CREATE UNIQUE INDEX IF NOT EXISTS uq_depositos_tx_hash_norm
    ON depositos ((LOWER(TRIM(tx_hash_tron))))
    WHERE tx_hash_tron IS NOT NULL AND TRIM(tx_hash_tron) <> '';

CREATE UNIQUE INDEX IF NOT EXISTS uq_users_tx_hash_verificacion_norm
    ON users ((LOWER(TRIM(tx_hash_verificacion))))
    WHERE tx_hash_verificacion IS NOT NULL AND TRIM(tx_hash_verificacion) <> '';

CREATE TABLE IF NOT EXISTS reconciliation_audit (
    id BIGSERIAL PRIMARY KEY,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    tx_hash TEXT NOT NULL,
    amount_micro BIGINT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_reconciliation_audit_tx_hash
    ON reconciliation_audit (tx_hash);

CREATE TABLE IF NOT EXISTS treasury_sweeps (
    id BIGSERIAL PRIMARY KEY,
    order_id TEXT NOT NULL UNIQUE,
    payin_address TEXT NOT NULL,
    vault_address TEXT NOT NULL,
    amount_usdt NUMERIC(18, 6) NOT NULL CHECK (amount_usdt > 0),
    tron_txid TEXT,
    status TEXT NOT NULL CHECK (status IN ('order_created', 'broadcasted', 'failed')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Aprobacion idempotente de deposito con bloqueo de fila (FOR UPDATE).
CREATE OR REPLACE FUNCTION aprobar_deposito(p_tx_hash TEXT)
RETURNS VOID
LANGUAGE plpgsql
AS $$
DECLARE
    v_dep depositos%ROWTYPE;
BEGIN
    SELECT *
    INTO v_dep
    FROM depositos
    WHERE LOWER(TRIM(tx_hash_tron)) = LOWER(TRIM(p_tx_hash))
      AND estado = 'PENDIENTE'
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN;
    END IF;

    UPDATE depositos
    SET estado = 'APROBADO',
        aprobado_at = NOW()
    WHERE id = v_dep.id;

    UPDATE users
    SET saldo_usdt = saldo_usdt + v_dep.monto_usdt
    WHERE id = v_dep.user_id;
END;
$$;

-- Activacion idempotente de verificacion de cuenta con bloqueo de fila.
CREATE OR REPLACE FUNCTION pagar_y_activar_verificacion(
    p_user_id UUID,
    p_metodo TEXT,
    p_tx_hash TEXT
)
RETURNS VOID
LANGUAGE plpgsql
AS $$
DECLARE
    v_verified BOOLEAN;
BEGIN
    SELECT cuenta_verificada
    INTO v_verified
    FROM users
    WHERE id = p_user_id
    FOR UPDATE;

    IF NOT FOUND OR v_verified THEN
        RETURN;
    END IF;

    UPDATE users
    SET cuenta_verificada = TRUE,
        metodo_verificacion = p_metodo,
        tx_hash_verificacion = TRIM(p_tx_hash),
        verificada_at = NOW()
    WHERE id = p_user_id;
END;
$$;
