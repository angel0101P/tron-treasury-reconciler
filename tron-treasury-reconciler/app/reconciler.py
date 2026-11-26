from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from psycopg.rows import dict_row

from .config import Settings, to_micro_usdt

logger = logging.getLogger("tron_treasury.reconciler")


def validate_trc20_transfer(
    tx: dict[str, Any],
    *,
    expected_to: str,
    expected_contract: str,
    min_amount_micro: int,
) -> tuple[bool, str, int]:
    """Strictly verify a TronGrid TRC-20 payload before accrediting funds."""
    raw_value = tx.get("value")
    try:
        received_micro = int(raw_value)
    except (TypeError, ValueError):
        return False, "invalid_value", 0

    if received_micro <= 0:
        return False, "non_positive_value", received_micro

    tx_to = str(tx.get("to") or "").strip()
    if tx_to != expected_to:
        return False, "recipient_mismatch", received_micro

    token_info = tx.get("token_info")
    if isinstance(token_info, dict) and token_info.get("address") is not None:
        contract = str(token_info.get("address") or "").strip()
        if contract != expected_contract:
            return False, "contract_mismatch", received_micro

    tx_type = tx.get("type")
    if tx_type is not None and str(tx_type).strip() != "Transfer":
        return False, "unexpected_tx_type", received_micro

    if received_micro < int(min_amount_micro):
        return False, "underpaid_amount", received_micro

    return True, "ok", received_micro


def _record_audit(
    conn: Any,
    *,
    entity_type: str,
    entity_id: str,
    tx_hash: str,
    amount_micro: int,
    status: str,
    reason: str,
) -> None:
    try:
        with conn.cursor() as cur:
            cur.execute("SAVEPOINT pf_audit")
            cur.execute(
                """
                INSERT INTO reconciliation_audit(
                    entity_type, entity_id, tx_hash, amount_micro, status, reason
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (entity_type, entity_id, tx_hash, int(amount_micro), status, reason),
            )
            cur.execute("RELEASE SAVEPOINT pf_audit")
    except Exception:
        try:
            with conn.cursor() as cur:
                cur.execute("ROLLBACK TO SAVEPOINT pf_audit")
        except Exception:
            pass


def reconcile_database(
    conn: Any,
    txs_by_hash: dict[str, dict[str, Any]],
    settings: Settings,
) -> dict[str, int]:
    """Reconcile pending deposits and account verifications against confirmed TRC-20 transfers."""
    summary = {
        "deposits_approved": 0,
        "users_verified": 0,
        "rejected": 0,
    }
    if not txs_by_hash:
        return summary

    used_hashes: set[str] = set()
    min_verif_micro = to_micro_usdt(settings.verification_amount_usdt)

    with conn.cursor(row_factory=dict_row) as cur:
        # 1. Pending deposits
        cur.execute(
            """
            SELECT id, monto_usdt, tx_hash_tron
            FROM depositos
            WHERE estado = 'PENDIENTE'
              AND tx_hash_tron IS NOT NULL
              AND TRIM(tx_hash_tron) <> ''
            """
        )
        pending_deposits = cur.fetchall()

        for dep in pending_deposits:
            raw_hash = str(dep["tx_hash_tron"]).strip()
            norm_hash = raw_hash.lower()
            tx = txs_by_hash.get(norm_hash)
            if not tx:
                continue

            if norm_hash in used_hashes:
                summary["rejected"] += 1
                _record_audit(
                    conn,
                    entity_type="deposit",
                    entity_id=str(dep["id"]),
                    tx_hash=norm_hash,
                    amount_micro=0,
                    status="rejected",
                    reason="duplicate_hash_in_cycle",
                )
                continue

            expected_micro = to_micro_usdt(Decimal(str(dep["monto_usdt"])))
            valid, reason, received_micro = validate_trc20_transfer(
                tx,
                expected_to=settings.tron_public_address,
                expected_contract=settings.usdt_contract,
                min_amount_micro=expected_micro,
            )
            if not valid:
                summary["rejected"] += 1
                logger.warning("Deposito %s rechazado (%s) tx=%s", dep["id"], reason, norm_hash)
                _record_audit(
                    conn,
                    entity_type="deposit",
                    entity_id=str(dep["id"]),
                    tx_hash=norm_hash,
                    amount_micro=received_micro,
                    status="rejected",
                    reason=reason,
                )
                continue

            cur.execute("SELECT aprobar_deposito(%s);", (raw_hash,))
            used_hashes.add(norm_hash)
            summary["deposits_approved"] += 1
            _record_audit(
                conn,
                entity_type="deposit",
                entity_id=str(dep["id"]),
                tx_hash=norm_hash,
                amount_micro=received_micro,
                status="approved",
                reason="ok",
            )
            conn.commit()
            logger.info("Deposito %s aprobado | tx=%s | micro=%s", dep["id"], norm_hash, received_micro)

        # 2. Account verifications
        cur.execute(
            """
            SELECT id, username, tx_hash_verificacion
            FROM users
            WHERE cuenta_verificada = FALSE
              AND tx_hash_verificacion IS NOT NULL
              AND TRIM(tx_hash_verificacion) <> ''
            """
        )
        pending_users = cur.fetchall()

        for usr in pending_users:
            raw_hash = str(usr["tx_hash_verificacion"]).strip()
            norm_hash = raw_hash.lower()
            tx = txs_by_hash.get(norm_hash)
            if not tx:
                continue

            if norm_hash in used_hashes:
                summary["rejected"] += 1
                _record_audit(
                    conn,
                    entity_type="user_verification",
                    entity_id=str(usr["id"]),
                    tx_hash=norm_hash,
                    amount_micro=0,
                    status="rejected",
                    reason="duplicate_hash_in_cycle",
                )
                continue

            valid, reason, received_micro = validate_trc20_transfer(
                tx,
                expected_to=settings.tron_public_address,
                expected_contract=settings.usdt_contract,
                min_amount_micro=min_verif_micro,
            )
            if not valid:
                summary["rejected"] += 1
                logger.warning("Verificacion %s rechazada (%s) tx=%s", usr["id"], reason, norm_hash)
                _record_audit(
                    conn,
                    entity_type="user_verification",
                    entity_id=str(usr["id"]),
                    tx_hash=norm_hash,
                    amount_micro=received_micro,
                    status="rejected",
                    reason=reason,
                )
                continue

            cur.execute(
                "SELECT pagar_y_activar_verificacion(%s::uuid, 'usdt_trc20', %s);",
                (str(usr["id"]), raw_hash),
            )
            used_hashes.add(norm_hash)
            summary["users_verified"] += 1
            _record_audit(
                conn,
                entity_type="user_verification",
                entity_id=str(usr["id"]),
                tx_hash=norm_hash,
                amount_micro=received_micro,
                status="approved",
                reason="ok",
            )
            conn.commit()
            logger.info("Cuenta verificada %s | tx=%s", usr["id"], norm_hash)

    return summary


def record_treasury_sweep(
    conn: Any,
    *,
    order_id: str,
    payin_address: str,
    vault_address: str,
    amount_usdt: Decimal,
    status: str,
) -> int | None:
    try:
        with conn.cursor() as cur:
            cur.execute("SAVEPOINT pf_sweep")
            cur.execute(
                """
                INSERT INTO treasury_sweeps(
                    order_id, payin_address, vault_address, amount_usdt, status
                )
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                (order_id, payin_address, vault_address, str(amount_usdt), status),
            )
            row = cur.fetchone()
            cur.execute("RELEASE SAVEPOINT pf_sweep")
            conn.commit()
            return int(row[0]) if row else None
    except Exception:
        try:
            with conn.cursor() as cur:
                cur.execute("ROLLBACK TO SAVEPOINT pf_sweep")
        except Exception:
            pass
        return None


def update_treasury_sweep(
    conn: Any,
    *,
    sweep_id: int | None,
    status: str,
    tron_txid: str | None = None,
) -> None:
    if sweep_id is None:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("SAVEPOINT pf_sweep_upd")
            cur.execute(
                """
                UPDATE treasury_sweeps
                SET status = %s,
                    tron_txid = COALESCE(%s, tron_txid)
                WHERE id = %s
                """,
                (status, tron_txid, int(sweep_id)),
            )
            cur.execute("RELEASE SAVEPOINT pf_sweep_upd")
            conn.commit()
    except Exception:
        try:
            with conn.cursor() as cur:
                cur.execute("ROLLBACK TO SAVEPOINT pf_sweep_upd")
        except Exception:
            pass
