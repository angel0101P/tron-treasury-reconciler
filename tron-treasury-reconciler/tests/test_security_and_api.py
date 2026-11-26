from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal

from fastapi.testclient import TestClient

from app import main as main_mod


class _DummyConn:
    def cursor(self, **_kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_health_exposes_no_balances_or_secrets(monkeypatch) -> None:
    monkeypatch.setenv("TRON_NETWORK", "nile")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("TICK_API_SECRET", "super-secret-value")

    with TestClient(main_mod.app) as client:
        res = client.get("/health")
        assert res.status_code == 200
        body = res.json()
        assert body == {
            "status": "online",
            "service": "tron-treasury-reconciler",
            "network": "nile",
            "dry_run": True,
        }
        assert "saldo_trx" not in body
        assert "saldo_usdt" not in body


def test_get_tick_is_rejected_with_405() -> None:
    with TestClient(main_mod.app) as client:
        res = client.get("/tick")
        assert res.status_code == 405


def test_post_tick_fails_closed_when_secret_missing(monkeypatch) -> None:
    monkeypatch.setenv("TICK_API_SECRET", "")
    with TestClient(main_mod.app) as client:
        res = client.post("/tick")
        assert res.status_code == 503


def test_post_tick_rejects_missing_or_wrong_secret(monkeypatch) -> None:
    monkeypatch.setenv("TICK_API_SECRET", "test-secret-123")
    with TestClient(main_mod.app) as client:
        res_missing = client.post("/tick")
        assert res_missing.status_code == 401

        res_wrong = client.post("/tick", headers={"X-Tick-Secret": "wrong-secret"})
        assert res_wrong.status_code == 401

        res_wrong_bearer = client.post("/tick", headers={"Authorization": "Bearer wrong"})
        assert res_wrong_bearer.status_code == 401


def test_post_tick_accepts_header_or_bearer_and_runs_cycle(monkeypatch) -> None:
    monkeypatch.setenv("TICK_API_SECRET", "test-secret-123")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost/db")
    monkeypatch.setenv("TRON_NETWORK", "nile")
    monkeypatch.setenv("DRY_RUN", "true")

    class _FakeTronService:
        def __init__(self, _settings):
            pass

        def fetch_confirmed_trc20_transfers(self):
            return {"0xabc": {"transaction_id": "0xabc"}}

        def get_balances(self):
            return Decimal("50.5"), 12_500_000

    @contextmanager
    def _fake_connect(_dsn):
        yield _DummyConn()

    monkeypatch.setattr(main_mod, "TronClientService", _FakeTronService)
    monkeypatch.setattr(main_mod.psycopg, "connect", _fake_connect)
    monkeypatch.setattr(
        main_mod,
        "reconcile_database",
        lambda _conn, _txs, _cfg: {"deposits_approved": 1, "users_verified": 0, "rejected": 0},
    )
    monkeypatch.setattr(
        main_mod,
        "sweep_to_arbitrum",
        lambda *_args, **_kwargs: {"swept": False, "dry_run": True},
    )

    with TestClient(main_mod.app) as client:
        res1 = client.post("/tick", headers={"X-Tick-Secret": "test-secret-123"})
        assert res1.status_code == 200
        body = res1.json()
        assert body["status"] == "ok"
        assert body["reconcile"]["deposits_approved"] == 1
        assert body["saldo_trx"] == 50.5
        assert body["saldo_usdt"] == 12.5

        res2 = client.post("/tick", headers={"Authorization": "Bearer test-secret-123"})
        assert res2.status_code == 200


def test_post_tick_returns_409_when_lock_busy(monkeypatch) -> None:
    monkeypatch.setenv("TICK_API_SECRET", "test-secret-123")
    assert main_mod.cycle_lock.acquire(blocking=False) is True
    try:
        with TestClient(main_mod.app) as client:
            res = client.post("/tick", headers={"X-Tick-Secret": "test-secret-123"})
            assert res.status_code == 409
    finally:
        main_mod.cycle_lock.release()


def test_post_tick_masks_internal_exceptions(monkeypatch) -> None:
    monkeypatch.setenv("TICK_API_SECRET", "test-secret-123")
    monkeypatch.setenv("DATABASE_URL", "postgresql://secret-dsn-should-not-leak")

    class _ExplodingTronService:
        def __init__(self, _settings):
            pass

        def fetch_confirmed_trc20_transfers(self):
            raise RuntimeError("sensitive internal connection failure detail")

    monkeypatch.setattr(main_mod, "TronClientService", _ExplodingTronService)

    with TestClient(main_mod.app) as client:
        res = client.post("/tick", headers={"X-Tick-Secret": "test-secret-123"})
        assert res.status_code == 500
        assert res.json() == {"detail": "Internal server error"}
        assert "sensitive internal" not in res.text
