from __future__ import annotations

from decimal import Decimal

import base58

from app.config import Settings, to_micro_usdt
from app.reconciler import reconcile_database, validate_trc20_transfer


def _tron_addr(seed: int) -> str:
    payload = b"\x41" + bytes([seed]) * 20
    return base58.b58encode_check(payload).decode("ascii")


EXPECTED_WALLET = _tron_addr(1)
OTHER_WALLET = _tron_addr(2)
USDT_CONTRACT = _tron_addr(3)
FAKE_CONTRACT = _tron_addr(4)


def _settings() -> Settings:
    return Settings(
        network="nile",
        dry_run=True,
        tick_api_secret="secret",
        tron_public_address=EXPECTED_WALLET,
        tron_private_key="",
        trongrid_api_key="",
        trongrid_base_url="https://nile.trongrid.io",
        usdt_contract=USDT_CONTRACT,
        wtrx_contract="",
        sunswap_router="",
        arbitrum_vault_address="0x" + "1" * 40,
        changenow_api_key="",
        database_url="",
        min_trx_gas=Decimal("35.0"),
        usdt_for_swap_gas=Decimal("10.0"),
        verification_amount_usdt=Decimal("10.0"),
        max_slippage_bps=150,
        log_level="INFO",
    )


def test_to_micro_usdt_uses_exact_decimal_math() -> None:
    assert to_micro_usdt("10.0") == 10_000_000
    assert to_micro_usdt(Decimal("10.000001")) == 10_000_001
    assert to_micro_usdt("0.01") == 10_000


def test_validate_trc20_transfer_checks_recipient_contract_type_and_amount() -> None:
    base_tx = {
        "transaction_id": "abc123",
        "to": EXPECTED_WALLET,
        "type": "Transfer",
        "value": "10000000",
        "token_info": {"address": USDT_CONTRACT},
    }

    ok, reason, amount = validate_trc20_transfer(
        base_tx,
        expected_to=EXPECTED_WALLET,
        expected_contract=USDT_CONTRACT,
        min_amount_micro=10_000_000,
    )
    assert ok is True and reason == "ok" and amount == 10_000_000

    # Wrong recipient
    bad_to = {**base_tx, "to": OTHER_WALLET}
    ok, reason, _ = validate_trc20_transfer(
        bad_to,
        expected_to=EXPECTED_WALLET,
        expected_contract=USDT_CONTRACT,
        min_amount_micro=10_000_000,
    )
    assert ok is False and reason == "recipient_mismatch"

    # Wrong token contract (spoofed TRC-20)
    bad_contract = {**base_tx, "token_info": {"address": FAKE_CONTRACT}}
    ok, reason, _ = validate_trc20_transfer(
        bad_contract,
        expected_to=EXPECTED_WALLET,
        expected_contract=USDT_CONTRACT,
        min_amount_micro=10_000_000,
    )
    assert ok is False and reason == "contract_mismatch"

    # Non-Transfer event
    bad_type = {**base_tx, "type": "Approval"}
    ok, reason, _ = validate_trc20_transfer(
        bad_type,
        expected_to=EXPECTED_WALLET,
        expected_contract=USDT_CONTRACT,
        min_amount_micro=10_000_000,
    )
    assert ok is False and reason == "unexpected_tx_type"

    # Underpaid by 1 micro-USDT
    underpaid = {**base_tx, "value": "9999999"}
    ok, reason, _ = validate_trc20_transfer(
        underpaid,
        expected_to=EXPECTED_WALLET,
        expected_contract=USDT_CONTRACT,
        min_amount_micro=10_000_000,
    )
    assert ok is False and reason == "underpaid_amount"


class _FakeCursor:
    def __init__(self, deposits, users, calls):
        self._deposits = deposits
        self._users = users
        self._calls = calls
        self._current_result = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql: str, params=None):
        self._calls.append((sql.strip(), params))
        if "FROM depositos" in sql:
            self._current_result = self._deposits
        elif "FROM users" in sql:
            self._current_result = self._users
        else:
            self._current_result = []

    def fetchall(self):
        return self._current_result


class _FakeConn:
    def __init__(self, deposits, users):
        self.deposits = deposits
        self.users = users
        self.calls = []
        self.commits = 0

    def cursor(self, **_kwargs):
        return _FakeCursor(self.deposits, self.users, self.calls)

    def commit(self):
        self.commits += 1


def test_reconcile_database_blocks_duplicate_hash_reuse_and_approves_valid() -> None:
    shared_hash = "0xdeadbeef"
    second_hash = "0xcafebabe"

    deposits = [
        {"id": 1, "monto_usdt": Decimal("25.00"), "tx_hash_tron": shared_hash},
        {"id": 2, "monto_usdt": Decimal("10.00"), "tx_hash_tron": second_hash},
    ]
    users = [
        # Attempts to reuse the same hash already claimed by deposit #1.
        {"id": "11111111-1111-1111-1111-111111111111", "username": "alice", "tx_hash_verificacion": shared_hash},
    ]
    txs = {
        shared_hash: {
            "transaction_id": shared_hash,
            "to": EXPECTED_WALLET,
            "type": "Transfer",
            "value": "25000000",
            "token_info": {"address": USDT_CONTRACT},
        },
        second_hash: {
            "transaction_id": second_hash,
            "to": EXPECTED_WALLET,
            "type": "Transfer",
            "value": "5000000",  # Underpaid for deposit #2 (expects 10 USDT)
            "token_info": {"address": USDT_CONTRACT},
        },
    }

    conn = _FakeConn(deposits, users)
    summary = reconcile_database(conn, txs, _settings())

    assert summary == {
        "deposits_approved": 1,
        "users_verified": 0,
        "rejected": 2,
    }
    approved_calls = [sql for sql, _p in conn.calls if "aprobar_deposito" in sql]
    verified_calls = [sql for sql, _p in conn.calls if "pagar_y_activar_verificacion" in sql]
    assert len(approved_calls) == 1
    assert len(verified_calls) == 0
