from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import requests

from .config import Settings, is_valid_evm_address, is_valid_tron_address


@dataclass(frozen=True, slots=True)
class ChangeNowOrder:
    order_id: str
    payin_address: str
    from_amount: Decimal


class ChangeNowClient:
    """HTTP client for ChangeNOW v2 cross-chain swaps (USDT TRC-20 -> ETH Arbitrum)."""

    base_url = "https://api.changenow.io/v2"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _headers(self) -> dict[str, str]:
        api_key = self.settings.changenow_api_key
        if not api_key:
            raise RuntimeError("CHANGENOW_API_KEY no esta configurada.")
        return {"x-changenow-api-key": api_key}

    def get_min_amount(self) -> Decimal:
        url = f"{self.base_url}/exchange/min-amount"
        params = {
            "fromCurrency": "usdt",
            "toCurrency": "eth",
            "fromNetwork": "trx",
            "toNetwork": "arb",
        }
        response = requests.get(url, params=params, headers=self._headers(), timeout=10)
        response.raise_for_status()
        raw = response.json().get("minAmount")
        if raw is None:
            raise ValueError("ChangeNOW no devolvio minAmount.")
        try:
            amount = Decimal(str(raw))
        except InvalidOperation as exc:
            raise ValueError(f"minAmount invalido en ChangeNOW: {raw!r}") from exc
        if amount <= 0:
            raise ValueError(f"minAmount debe ser positivo: {amount}")
        return amount

    def create_exchange_order(self, amount_usdt: Decimal) -> ChangeNowOrder:
        vault = self.settings.arbitrum_vault_address
        if not is_valid_evm_address(vault):
            raise ValueError(f"ARBITRUM_VAULT_ADDRESS invalida: {vault!r}")
        if amount_usdt <= 0:
            raise ValueError("El monto de la orden ChangeNOW debe ser positivo.")

        url = f"{self.base_url}/exchange"
        payload = {
            "fromCurrency": "usdt",
            "toCurrency": "eth",
            "fromNetwork": "trx",
            "toNetwork": "arb",
            "fromAmount": str(amount_usdt),
            "address": vault,
        }
        response = requests.post(url, json=payload, headers=self._headers(), timeout=15)
        response.raise_for_status()
        data = response.json()

        order_id = str(data.get("id") or "").strip()
        payin_address = str(data.get("payinAddress") or "").strip()

        if not order_id:
            raise ValueError("ChangeNOW no devolvio un id de orden valido.")
        if not is_valid_tron_address(payin_address):
            raise ValueError(f"ChangeNOW devolvio una payinAddress Tron invalida: {payin_address!r}")

        return ChangeNowOrder(
            order_id=order_id,
            payin_address=payin_address,
            from_amount=amount_usdt,
        )
