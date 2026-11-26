from __future__ import annotations

import logging
from decimal import Decimal, ROUND_DOWN
from typing import Any

from .changenow import ChangeNowClient
from .config import Settings, to_micro_usdt
from .reconciler import record_treasury_sweep, update_treasury_sweep
from .tron_client import TronClientService

logger = logging.getLogger("tron_treasury.treasury")


def sweep_to_arbitrum(
    tron_service: TronClientService,
    changenow_client: ChangeNowClient,
    settings: Settings,
    *,
    conn: Any | None = None,
) -> dict[str, Any]:
    """Sweep available USDT (TRC-20) to Arbitrum vault via ChangeNOW v2."""
    _trx_balance, usdt_micro = tron_service.get_balances()
    balance_usdt = Decimal(usdt_micro) / Decimal(1_000_000)

    if balance_usdt <= 0:
        return {"swept": False, "reason": "Saldo USDT en 0"}

    min_cn = changenow_client.get_min_amount()
    if balance_usdt < min_cn:
        return {
            "swept": False,
            "reason": f"Saldo ({balance_usdt:.2f} USDT) menor al minimo ({min_cn} USDT)",
        }

    gas_status = tron_service.ensure_trx_gas()
    if not gas_status.get("ok"):
        return {"swept": False, "reason": "TRX insuficiente para operar"}

    _trx_after, usdt_micro_after = tron_service.get_balances()
    current_usdt = Decimal(usdt_micro_after) / Decimal(1_000_000)
    amount_to_send = current_usdt.quantize(Decimal("0.01"), rounding=ROUND_DOWN)

    if amount_to_send < min_cn:
        return {
            "swept": False,
            "reason": f"Saldo post-gas ({amount_to_send} USDT) menor al minimo ({min_cn} USDT)",
        }

    if settings.dry_run:
        logger.info(
            "[DRY_RUN] Barrido simulado: %s USDT -> %s",
            amount_to_send,
            settings.arbitrum_vault_address,
        )
        return {
            "swept": False,
            "dry_run": True,
            "would_sweep_usdt": str(amount_to_send),
            "vault": settings.arbitrum_vault_address,
        }

    order = changenow_client.create_exchange_order(amount_to_send)
    sweep_id = (
        record_treasury_sweep(
            conn,
            order_id=order.order_id,
            payin_address=order.payin_address,
            vault_address=settings.arbitrum_vault_address,
            amount_usdt=amount_to_send,
            status="order_created",
        )
        if conn is not None
        else None
    )

    try:
        txid = tron_service.send_usdt(order.payin_address, to_micro_usdt(amount_to_send))
    except Exception:
        if conn is not None:
            update_treasury_sweep(conn, sweep_id=sweep_id, status="failed", tron_txid=None)
        raise

    if conn is not None:
        update_treasury_sweep(conn, sweep_id=sweep_id, status="broadcasted", tron_txid=txid)

    logger.info(
        "Barrido enviado a Arbitrum: %s USDT | order=%s | payin=%s | txid=%s",
        amount_to_send,
        order.order_id,
        order.payin_address,
        txid,
    )
    return {
        "swept": True,
        "order_id": order.order_id,
        "amount_usdt": str(amount_to_send),
        "txid": txid,
    }
