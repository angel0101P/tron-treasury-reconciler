from __future__ import annotations

import os
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import base58
from dotenv import load_dotenv

EVM_ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
MICRO_USDT_FACTOR = Decimal("1000000")

NETWORK_PRESETS: dict[str, dict[str, str]] = {
    "mainnet": {
        "trongrid_base_url": "https://api.trongrid.io",
        "usdt_contract": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
        "wtrx_contract": "TNUC9Qb1rRpS5CbWLmNMxXBjyFoydXjWFR",
        "sunswap_router": "TKzxdSv2FZKQrEqkKVgp5DcwEXBEKMg2Ax",
    },
    "nile": {
        "trongrid_base_url": "https://nile.trongrid.io",
        "usdt_contract": "TXYZopYRdj2D9XRtbG411XZZ3kM5VkAeBf",
        "wtrx_contract": "TYsbWxNnyTgsZaTFaue9hqpxkU3Fkco94a",
        "sunswap_router": "",
    },
    "shasta": {
        "trongrid_base_url": "https://api.shasta.trongrid.io",
        "usdt_contract": "TG3XXyExBkPp9nzdajDZsozEu4BkaSJozs",
        "wtrx_contract": "",
        "sunswap_router": "",
    },
}


def to_micro_usdt(amount: Decimal | str | int | float) -> int:
    """Convert a USDT amount into exact integer micro-units (10^-6 USDT)."""
    try:
        dec = amount if isinstance(amount, Decimal) else Decimal(str(amount).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Monto USDT invalido: {amount!r}") from exc

    if not dec.is_finite() or dec < 0:
        raise ValueError(f"Monto USDT debe ser finito y no negativo: {amount!r}")

    scaled = (dec * MICRO_USDT_FACTOR).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(scaled)


def is_valid_tron_address(address: str) -> bool:
    """Validate Tron Base58Check address format (prefix T, 34 chars, valid checksum)."""
    raw = str(address or "").strip()
    if not raw.startswith("T") or len(raw) != 34:
        return False
    try:
        decoded = base58.b58decode_check(raw)
    except Exception:
        return False
    return len(decoded) == 21 and decoded[0] == 0x41


def is_valid_evm_address(address: str) -> bool:
    """Validate EVM hex address format (0x + 40 hex chars)."""
    return bool(EVM_ADDRESS_RE.fullmatch(str(address or "").strip()))


def clamp_slippage_bps(bps: int) -> int:
    """Clamp DEX slippage tolerance to [10, 500] basis points (0.10% - 5.00%)."""
    return max(10, min(500, int(bps)))


@dataclass(slots=True)
class Settings:
    network: str
    dry_run: bool
    tick_api_secret: str
    tron_public_address: str
    tron_private_key: str
    trongrid_api_key: str
    trongrid_base_url: str
    usdt_contract: str
    wtrx_contract: str
    sunswap_router: str
    arbitrum_vault_address: str
    changenow_api_key: str
    database_url: str
    min_trx_gas: Decimal
    usdt_for_swap_gas: Decimal
    verification_amount_usdt: Decimal
    max_slippage_bps: int
    log_level: str


def _parse_bool(value: str, default: bool) -> bool:
    raw = value.strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _parse_decimal(name: str, default: str) -> Decimal:
    raw = os.getenv(name, default).strip() or default
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"Variable de entorno invalida {name}={raw!r}") from exc
    if not value.is_finite() or value < 0:
        raise ValueError(f"Variable {name} debe ser no negativa: {raw!r}")
    return value


def load_settings() -> Settings:
    load_dotenv()

    network = os.getenv("TRON_NETWORK", "nile").strip().lower()
    if network not in NETWORK_PRESETS:
        raise ValueError(
            f"TRON_NETWORK invalida: {network!r}. Valores soportados: {', '.join(NETWORK_PRESETS)}."
        )
    preset = NETWORK_PRESETS[network]

    trongrid_base_url = (os.getenv("TRONGRID_BASE_URL", "").strip() or preset["trongrid_base_url"]).rstrip("/")
    usdt_contract = os.getenv("TRON_USDT_CONTRACT", "").strip() or preset["usdt_contract"]
    wtrx_contract = os.getenv("TRON_WTRX_CONTRACT", "").strip() or preset["wtrx_contract"]
    sunswap_router = os.getenv("TRON_SUNSWAP_ROUTER", "").strip() or preset["sunswap_router"]

    raw_bps = int(os.getenv("MAX_SLIPPAGE_BPS", "150").strip() or "150")

    return Settings(
        network=network,
        dry_run=_parse_bool(os.getenv("DRY_RUN", "true"), default=True),
        tick_api_secret=os.getenv("TICK_API_SECRET", "").strip(),
        tron_public_address=os.getenv("TRON_PUBLIC_ADDRESS", "").strip(),
        tron_private_key=os.getenv("TRON_PRIVATE_KEY", "").replace("0x", "").strip(),
        trongrid_api_key=os.getenv("TRONGRID_API_KEY", "").strip(),
        trongrid_base_url=trongrid_base_url,
        usdt_contract=usdt_contract,
        wtrx_contract=wtrx_contract,
        sunswap_router=sunswap_router,
        arbitrum_vault_address=os.getenv("ARBITRUM_VAULT_ADDRESS", "").strip(),
        changenow_api_key=os.getenv("CHANGENOW_API_KEY", "").strip(),
        database_url=os.getenv("DATABASE_URL", "").strip(),
        min_trx_gas=_parse_decimal("MIN_TRX_GAS", "35.0"),
        usdt_for_swap_gas=_parse_decimal("USDT_PARA_SWAP_GAS", "10.0"),
        verification_amount_usdt=_parse_decimal("MONTO_VERIFICACION_USDT", "10.0"),
        max_slippage_bps=clamp_slippage_bps(raw_bps),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
    )
