from __future__ import annotations

from decimal import Decimal

import base58
import pytest

from app.changenow import ChangeNowClient
from app.config import Settings, is_valid_evm_address, is_valid_tron_address
from app.treasury import sweep_to_arbitrum
from app.tron_client import calculate_min_out


def _tron_addr(seed: int) -> str:
    payload = b"\x41" + bytes([seed]) * 20
    return base58.b58encode_check(payload).decode("ascii")


WALLET = _tron_addr(10)
PAYIN_WALLET = _tron_addr(11)
VAULT_EVM = "0x" + "a" * 40


def _settings(*, dry_run: bool = True) -> Settings:
    return Settings(
        network="nile",
        dry_run=dry_run,
        tick_api_secret="secret",
        tron_public_address=WALLET,
        tron_private_key="",
        trongrid_api_key="",
        trongrid_base_url="https://nile.trongrid.io",
        usdt_contract=_tron_addr(12),
        wtrx_contract=_tron_addr(13),
        sunswap_router=_tron_addr(14),
        arbitrum_vault_address=VAULT_EVM,
        changenow_api_key="cn-test-key",
        database_url="",
        min_trx_gas=Decimal("35.0"),
        usdt_for_swap_gas=Decimal("10.0"),
        verification_amount_usdt=Decimal("10.0"),
        max_slippage_bps=150,
        log_level="INFO",
    )


def test_address_validators_check_base58check_and_evm() -> None:
    assert is_valid_tron_address(WALLET) is True
    # Corrupt last character (fails Base58Check checksum even though length is 34 and starts with T).
    corrupted = WALLET[:-1] + ("A" if WALLET[-1] != "A" else "B")
    assert len(corrupted) == 34 and corrupted.startswith("T")
    assert is_valid_tron_address(corrupted) is False
    assert is_valid_tron_address("0x1234") is False

    assert is_valid_evm_address(VAULT_EVM) is True
    assert is_valid_evm_address("0x123") is False
    assert is_valid_evm_address(WALLET) is False


def test_calculate_min_out_protects_against_mev_and_zero_quotes() -> None:
    # 100_000_000 SUN expected, 150 bps (1.5%) slippage -> 98_500_000 SUN minimum
    assert calculate_min_out(100_000_000, 150) == 98_500_000

    with pytest.raises(ValueError):
        calculate_min_out(0, 150)

    with pytest.raises(ValueError):
        calculate_min_out(1, 150)


def test_changenow_rejects_invalid_payin_or_vault_address(monkeypatch) -> None:
    cfg = _settings(dry_run=False)
    client = ChangeNowClient(cfg)

    class _BadResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"id": "ord-1", "payinAddress": "TInvalidAddress1234567890123456789"}

    monkeypatch.setattr("app.changenow.requests.post", lambda *_a, **_kw: _BadResp())
    with pytest.raises(ValueError, match="payinAddress Tron invalida"):
        client.create_exchange_order(Decimal("25.00"))

    bad_vault_cfg = _settings(dry_run=False)
    bad_vault_cfg.arbitrum_vault_address = "not-an-evm-address"
    with pytest.raises(ValueError, match="ARBITRUM_VAULT_ADDRESS invalida"):
        ChangeNowClient(bad_vault_cfg).create_exchange_order(Decimal("25.00"))


class _StubTronService:
    def __init__(self, trx: Decimal, usdt_micro: int) -> None:
        self.trx = trx
        self.usdt_micro = usdt_micro
        self.sent: list[tuple[str, int]] = []

    def get_balances(self):
        return self.trx, self.usdt_micro

    def ensure_trx_gas(self):
        return {"ok": True, "swapped": False}

    def send_usdt(self, destination: str, amount_micro: int) -> str:
        self.sent.append((destination, amount_micro))
        return "tx-sweep-001"


class _StubChangeNow:
    def __init__(self, min_amount: Decimal) -> None:
        self.min_amount = min_amount
        self.orders_created = 0

    def get_min_amount(self) -> Decimal:
        return self.min_amount

    def create_exchange_order(self, amount_usdt: Decimal):
        self.orders_created += 1
        from app.changenow import ChangeNowOrder

        return ChangeNowOrder(order_id="cn-order-1", payin_address=PAYIN_WALLET, from_amount=amount_usdt)


def test_sweep_dry_run_never_creates_order_or_broadcasts() -> None:
    tron = _StubTronService(Decimal("60.0"), 42_567_890)
    cn = _StubChangeNow(Decimal("15.0"))
    res = sweep_to_arbitrum(tron, cn, _settings(dry_run=True))

    assert res["swept"] is False
    assert res["dry_run"] is True
    assert res["would_sweep_usdt"] == "42.56"  # Quantized DOWN cleanly
    assert cn.orders_created == 0
    assert tron.sent == []


def test_sweep_live_rounds_down_and_broadcasts_exact_micro_units() -> None:
    tron = _StubTronService(Decimal("60.0"), 42_567_890)
    cn = _StubChangeNow(Decimal("15.0"))
    res = sweep_to_arbitrum(tron, cn, _settings(dry_run=False))

    assert res == {
        "swept": True,
        "order_id": "cn-order-1",
        "amount_usdt": "42.56",
        "txid": "tx-sweep-001",
    }
    assert cn.orders_created == 1
    assert tron.sent == [(PAYIN_WALLET, 42_560_000)]
