from __future__ import annotations

import hmac
import logging
import os
import threading
from contextlib import asynccontextmanager
from typing import Any

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException

from .changenow import ChangeNowClient
from .config import load_settings
from .reconciler import reconcile_database
from .treasury import sweep_to_arbitrum
from .tron_client import TronClientService

logger = logging.getLogger("tron_treasury.api")
cycle_lock = threading.Lock()


def _configure_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        )
    else:
        logging.getLogger().setLevel(level)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        cfg = load_settings()
        _configure_logging(cfg.log_level)
        logger.info("Servicio iniciado | network=%s | dry_run=%s", cfg.network, cfg.dry_run)
    except Exception:
        _configure_logging("INFO")
        logger.warning("Servicio iniciado con configuracion parcial; verifica variables de entorno.")
    yield
    logger.info("Servicio detenido.")


app = FastAPI(
    title="Tron Treasury Reconciler",
    version="1.0.0",
    description="Servicio de conciliacion on-chain TRC-20 y tesoreria automatizada Tron -> Arbitrum.",
    lifespan=lifespan,
)


def require_tick_auth(
    x_tick_secret: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> None:
    expected = os.getenv("TICK_API_SECRET", "").strip()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="TICK_API_SECRET is not configured on server",
        )

    provided = (x_tick_secret or "").strip()
    if not provided and authorization:
        raw = authorization.strip()
        if raw.lower().startswith("bearer "):
            provided = raw[7:].strip()

    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing tick credentials",
        )


@app.get("/")
@app.get("/health")
def health() -> dict[str, Any]:
    cfg = load_settings()
    return {
        "status": "online",
        "service": "tron-treasury-reconciler",
        "network": cfg.network,
        "dry_run": cfg.dry_run,
    }


@app.post("/tick", dependencies=[Depends(require_tick_auth)])
def ejecutar_tick() -> dict[str, Any]:
    if not cycle_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Cycle already running")

    try:
        cfg = load_settings()
        if not cfg.database_url:
            raise RuntimeError("DATABASE_URL no esta configurada.")

        tron_service = TronClientService(cfg)
        changenow_client = ChangeNowClient(cfg)

        txs_red = tron_service.fetch_confirmed_trc20_transfers()
        with psycopg.connect(cfg.database_url) as conn:
            reconcile_summary = reconcile_database(conn, txs_red, cfg)
            sweep_result = sweep_to_arbitrum(
                tron_service,
                changenow_client,
                cfg,
                conn=conn,
            )

        trx_balance, usdt_micro = tron_service.get_balances()
        usdt_display = round(usdt_micro / 1_000_000, 2)

        return {
            "status": "ok",
            "network": cfg.network,
            "dry_run": cfg.dry_run,
            "reconcile": reconcile_summary,
            "saldo_trx": round(float(trx_balance), 2),
            "saldo_usdt": usdt_display,
            "puente_arbitrum": sweep_result,
        }
    except HTTPException:
        raise
    except Exception:
        logger.exception("Error inesperado ejecutando /tick")
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        cycle_lock.release()
