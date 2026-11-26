from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Any

import requests
from tronpy import Tron
from tronpy.keys import PrivateKey
from tronpy.providers import HTTPProvider

from .config import Settings, clamp_slippage_bps, is_valid_tron_address, to_micro_usdt

logger = logging.getLogger("tron_treasury.tron")


def calculate_min_out(expected_out: int, slippage_bps: int) -> int:
    """Compute slippage-protected minimum output amount; never allow min_out <= 1."""
    if int(expected_out) <= 0:
        raise ValueError(f"Cotizacion DEX invalida (expected_out={expected_out}).")
    bps = clamp_slippage_bps(slippage_bps)
    min_out = (int(expected_out) * (10_000 - bps)) // 10_000
    if min_out <= 1:
        raise ValueError(
            f"Monto minimo post-slippage demasiado bajo ({min_out}); abortando swap para evitar MEV."
        )
    return min_out


class TronClientService:
    """Encapsulates TronGrid REST queries and lazy-initialized TronPy signing."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: Tron | None = None
        self._private_key: PrivateKey | None = None

    def _get_client(self) -> Tron:
        if self._client is not None:
            return self._client

        api_key = self.settings.trongrid_api_key or None
        endpoint = self.settings.trongrid_base_url
        provider = HTTPProvider(endpoint_uri=endpoint, api_key=api_key)
        self._client = Tron(provider=provider)
        return self._client

    def _get_private_key(self) -> PrivateKey:
        if self._private_key is not None:
            return self._private_key

        raw_key = self.settings.tron_private_key
        if not raw_key:
            raise RuntimeError("TRON_PRIVATE_KEY no esta configurada.")
        try:
            self._private_key = PrivateKey(bytes.fromhex(raw_key))
        except Exception as exc:
            raise RuntimeError("TRON_PRIVATE_KEY invalida (se requiere hex de 64 caracteres).") from exc
        return self._private_key

    def _ensure_public_address(self) -> str:
        addr = self.settings.tron_public_address
        if not is_valid_tron_address(addr):
            raise RuntimeError("TRON_PUBLIC_ADDRESS invalida o no configurada.")
        return addr

    def get_balances(self) -> tuple[Decimal, int]:
        """Return (trx_balance, usdt_micro_balance)."""
        addr = self._ensure_public_address()
        client = self._get_client()
        saldo_trx = Decimal(str(client.get_account_balance(addr)))
        contrato_usdt = client.get_contract(self.settings.usdt_contract)
        saldo_usdt_micro = int(contrato_usdt.functions.balanceOf(addr))
        return saldo_trx, saldo_usdt_micro

    def fetch_confirmed_trc20_transfers(self, *, limit: int = 100) -> dict[str, dict[str, Any]]:
        """Query TronGrid REST directly for confirmed incoming TRC-20 transfers."""
        addr = self._ensure_public_address()
        if not is_valid_tron_address(self.settings.usdt_contract):
            raise RuntimeError("TRON_USDT_CONTRACT invalido o no configurado.")

        url = f"{self.settings.trongrid_base_url}/v1/accounts/{addr}/transactions/trc20"
        headers = {"TRON-PRO-API-KEY": self.settings.trongrid_api_key} if self.settings.trongrid_api_key else {}
        params = {
            "only_to": "true",
            "only_confirmed": "true",
            "contract_address": self.settings.usdt_contract,
            "limit": max(1, min(int(limit), 200)),
        }
        response = requests.get(url, params=params, headers=headers, timeout=15)
        response.raise_for_status()
        payload = response.json()
        rows = payload.get("data", [])
        if not isinstance(rows, list):
            return {}

        indexed: dict[str, dict[str, Any]] = {}
        for tx in rows:
            if not isinstance(tx, dict):
                continue
            tx_id = str(tx.get("transaction_id", "")).strip().lower()
            if tx_id:
                indexed[tx_id] = tx
        return indexed

    def ensure_trx_gas(self) -> dict[str, Any]:
        """Ensure minimum TRX gas reserve; swap exact USDT on SunSwap with slippage guard if needed."""
        trx_balance, usdt_micro = self.get_balances()
        if trx_balance >= self.settings.min_trx_gas:
            return {
                "ok": True,
                "swapped": False,
                "trx_balance": str(trx_balance),
            }

        required_usdt_micro = to_micro_usdt(self.settings.usdt_for_swap_gas + Decimal("2.0"))
        if usdt_micro < required_usdt_micro:
            logger.warning(
                "TRX bajo (%s TRX) y USDT insuficiente (%s micro-USDT) para auto-swap.",
                trx_balance,
                usdt_micro,
            )
            return {
                "ok": False,
                "swapped": False,
                "reason": "insufficient_usdt_for_gas_swap",
            }

        if self.settings.dry_run:
            logger.info(
                "[DRY_RUN] TRX bajo (%s < %s). Se omite swap real de %s USDT.",
                trx_balance,
                self.settings.min_trx_gas,
                self.settings.usdt_for_swap_gas,
            )
            return {
                "ok": True,
                "swapped": False,
                "dry_run": True,
                "would_swap_usdt": str(self.settings.usdt_for_swap_gas),
            }

        addr = self._ensure_public_address()
        if not is_valid_tron_address(self.settings.sunswap_router) or not is_valid_tron_address(
            self.settings.wtrx_contract
        ):
            raise RuntimeError(
                "TRON_SUNSWAP_ROUTER y TRON_WTRX_CONTRACT deben estar configurados con direcciones validas."
            )

        priv_key = self._get_private_key()
        client = self._get_client()
        amount_in_micro = to_micro_usdt(self.settings.usdt_for_swap_gas)
        contrato_usdt = client.get_contract(self.settings.usdt_contract)
        router = client.get_contract(self.settings.sunswap_router)

        path = [self.settings.usdt_contract, self.settings.wtrx_contract]
        amounts_out = router.functions.getAmountsOut(amount_in_micro, path)
        if not isinstance(amounts_out, (list, tuple)) or len(amounts_out) < 2:
            raise RuntimeError("SunSwap getAmountsOut devolvio una respuesta inesperada.")
        expected_trx_sun = int(amounts_out[-1])
        min_trx_out = calculate_min_out(expected_trx_sun, self.settings.max_slippage_bps)

        allowance = int(contrato_usdt.functions.allowance(addr, self.settings.sunswap_router))
        if allowance < amount_in_micro:
            # Approve only the exact swap input instead of unbounded allowance.
            tx_approve = (
                contrato_usdt.functions.approve(self.settings.sunswap_router, amount_in_micro)
                .with_owner(addr)
                .fee_limit(100_000_000)
                .build()
                .sign(priv_key)
            )
            tx_approve.broadcast().wait()
            time.sleep(3)

        deadline = int(time.time()) + 300
        tx_swap = (
            router.functions.swapExactTokensForETH(
                amount_in_micro,
                min_trx_out,
                path,
                addr,
                deadline,
            )
            .with_owner(addr)
            .fee_limit(150_000_000)
            .build()
            .sign(priv_key)
        )
        res = tx_swap.broadcast().wait()
        txid = str(res.get("id", ""))
        logger.info("Gas TRX repuesto via SunSwap. TxID=%s min_out_sun=%s", txid, min_trx_out)
        return {
            "ok": True,
            "swapped": True,
            "txid": txid,
            "min_out_sun": min_trx_out,
        }

    def send_usdt(self, destination: str, amount_micro: int) -> str:
        """Send exact micro-USDT units to a validated Tron address."""
        if not is_valid_tron_address(destination):
            raise ValueError(f"Direccion destino Tron invalida: {destination!r}")
        if int(amount_micro) <= 0:
            raise ValueError(f"El monto en micro-USDT debe ser positivo: {amount_micro}")

        if self.settings.dry_run:
            logger.info("[DRY_RUN] Simulacion send_usdt -> destino=%s micro=%s", destination, amount_micro)
            return "dry-run-txid"

        addr = self._ensure_public_address()
        priv_key = self._get_private_key()
        client = self._get_client()
        contrato = client.get_contract(self.settings.usdt_contract)

        txn = (
            contrato.functions.transfer(destination, int(amount_micro))
            .with_owner(addr)
            .fee_limit(150_000_000)
            .build()
            .sign(priv_key)
        )
        res = txn.broadcast()
        txid = str(res.get("txid") or "").strip()
        if not txid:
            raise RuntimeError(f"Error al transmitir USDT: {res}")
        return txid
