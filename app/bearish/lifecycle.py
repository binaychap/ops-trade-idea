from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
import importlib
import logging
import threading
import time
from typing import Any
from uuid import uuid4

from app.persistence.ledger import Ledger


logger = logging.getLogger(__name__)
_worker_guard = threading.Lock()
_worker_paths: set[str] = set()
_detail_lock = threading.Lock()
_next_detail_at = 0.0
_KNOWN_STATUSES = {"PENDING", "SUBMITTED", "PARTIAL_FILLED", "FILLED", "CANCELLED", "FAILED"}
_TERMINAL_STATUSES = {"FILLED", "CANCELLED", "FAILED"}
_MARKET_ORDER_UNSUPPORTED = "OPENAPI_OPTION_NOT_ALLOW_PLACING_MARKET_ORDER"
_ORDER_NOT_PRESENT = "OPENAPI_PARAM_ERR"
_MAX_ORDER_NOT_PRESENT_RETRIES = 3


def _new_order_id() -> str:
    return uuid4().hex


def _is_market_order_unsupported(exc: BaseException | str) -> bool:
    return (
        getattr(exc, "error_code", None) == _MARKET_ORDER_UNSUPPORTED
        or _MARKET_ORDER_UNSUPPORTED in str(exc)
    )


def _is_order_not_present(exc: BaseException) -> bool:
    return (
        getattr(exc, "error_code", None) == _ORDER_NOT_PRESENT
        and "order not present" in str(getattr(exc, "error_msg", exc)).lower()
    )


def _response_data(response: Any) -> Any:
    if getattr(response, "status_code", None) != 200:
        raise RuntimeError(f"Webull order request failed with HTTP {getattr(response, 'status_code', 'unknown')}")
    data = response.json()
    if isinstance(data, dict) and data.get("error_code"):
        raise RuntimeError(f"Webull order request failed: {data['error_code']}")
    return data


def _orders_from_response(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict):
        orders = data.get("orders")
        if isinstance(orders, list):
            return [order for order in orders if isinstance(order, dict)]
        return [data]
    if isinstance(data, list):
        return [order for order in data if isinstance(order, dict)]
    return []


class BearishPutLifecycle:
    """Persist and reconcile a market PUT entry before attaching broker exits."""

    def __init__(
        self,
        ledger: Ledger,
        *,
        trade_client: Any | None = None,
        option_module: Any | None = None,
    ) -> None:
        self.ledger = ledger
        self.trade_client = trade_client
        self.option_module = option_module

    def _module(self) -> Any:
        if self.option_module is None:
            self.option_module = importlib.import_module("app.options.brackets")
        return self.option_module

    def _client(self) -> Any:
        if self.trade_client is None:
            self.trade_client = self._module().get_trade_client()
        return self.trade_client

    def submit_entry(
        self,
        *,
        job_id: str,
        trade_id: str,
        account_id: str,
        symbol: str,
        desired_strike: float,
        quantity: int,
        profit_percent: float,
        stop_loss_percent: float,
        stop_loss_enabled: bool,
    ) -> dict[str, Any]:
        symbol = str(symbol).strip().upper()
        if not symbol or quantity <= 0:
            raise ValueError("A symbol and positive PUT quantity are required")

        existing = self.ledger.bearish_option_job(job_id)
        if existing is not None:
            if (existing.get("status") == "entry_submitting"
                    and _is_market_order_unsupported(
                        existing.get("submission_error") or existing.get("last_error", "")
                    )):
                existing["status"] = "entry_rejected"
                existing["next_attempt_at"] = None
                existing["rejection_code"] = _MARKET_ORDER_UNSUPPORTED
                self.ledger.save_bearish_option_job(job_id, existing)
                logger.warning(
                    "Marked previously uncertain bearish PUT entry rejected for %s (%s %s): %s",
                    symbol,
                    existing.get("contract_symbol"),
                    existing.get("expiration"),
                    existing["last_error"],
                )
            return existing

        expiration, strike, contract_symbol = self._module()._find_valid_contract(
            symbol,
            None,
            desired_strike,
            option_type="PUT",
        )
        if not contract_symbol:
            raise RuntimeError("Webull did not return a symbol for the selected PUT contract")

        entry_client_order_id = _new_order_id()
        job = {
            "job_id": job_id,
            "trade_id": trade_id,
            "account_id": account_id,
            "symbol": symbol,
            "contract_symbol": contract_symbol,
            "strike": float(strike),
            "expiration": expiration,
            "quantity": int(quantity),
            "profit_percent": float(profit_percent),
            "stop_loss_percent": float(stop_loss_percent),
            "stop_loss_enabled": bool(stop_loss_enabled),
            "entry_client_order_id": entry_client_order_id,
            "entry_order_id": None,
            "entry_order": {
                "client_order_id": entry_client_order_id,
                "combo_type": "NORMAL",
                "option_strategy": "SINGLE",
                "instrument_type": "OPTION",
                "market": "US",
                "symbol": symbol,
                "order_type": "MARKET",
                "quantity": str(quantity),
                "side": "BUY",
                "position_intent": "BUY_TO_OPEN",
                "time_in_force": "DAY",
                "entrust_type": "QTY",
                "legs": [self._option_leg(symbol, strike, expiration, "BUY", quantity)],
            },
            "exit_combo_order_id": _new_order_id(),
            "profit_client_order_id": _new_order_id(),
            "stop_client_order_id": _new_order_id() if stop_loss_enabled else None,
            "status": "entry_submitting",
            "retry_count": 0,
            "next_attempt_at": datetime.now(UTC).isoformat(),
        }
        if not self.ledger.register_bearish_option_job(job_id, job):
            saved = self.ledger.bearish_option_job(job_id)
            if saved is None:
                raise RuntimeError("Bearish PUT entry reservation disappeared")
            return saved

        try:
            response = self._client().order_v3.place_order(account_id, [job["entry_order"]])
            data = _response_data(response)
            order = next(
                (item for item in _orders_from_response(data)
                 if item.get("client_order_id") == entry_client_order_id),
                {},
            )
            job["entry_order_id"] = order.get("order_id")
            job["status"] = "entry_pending"
            job["last_error"] = None
            self.ledger.save_bearish_option_job(job_id, job)
        except Exception as exc:
            job["last_error"] = str(exc)[:500]
            job["submission_error"] = job["last_error"]
            if _is_market_order_unsupported(exc):
                job["status"] = "entry_rejected"
                job["next_attempt_at"] = None
                job["rejection_code"] = _MARKET_ORDER_UNSUPPORTED
                self.ledger.save_bearish_option_job(job_id, job)
                logger.warning(
                    "Webull rejected bearish PUT market entry for %s (%s %s): %s",
                    symbol,
                    contract_symbol,
                    expiration,
                    job["last_error"],
                )
                return job
            job["next_attempt_at"] = (datetime.now(UTC) + timedelta(seconds=2)).isoformat()
            self.ledger.save_bearish_option_job(job_id, job)
            logger.exception("Bearish PUT entry result is uncertain for trade_id=%s", trade_id)
        return job

    @staticmethod
    def _option_leg(symbol: str, strike: float, expiration: str, side: str, quantity: int) -> dict[str, str]:
        return {
            "side": side,
            "quantity": str(quantity),
            "symbol": symbol,
            "strike_price": f"{float(strike):.2f}",
            "option_expire_date": expiration,
            "instrument_type": "OPTION",
            "option_type": "PUT",
            "market": "US",
        }

    def reconcile(self, job: dict[str, Any]) -> None:
        if job.get("status") in {
            "entry_rejected", "entry_no_fill", "entry_unresolved", "exits_active",
        }:
            return
        if (job.get("status") == "entry_submitting"
                and _is_market_order_unsupported(
                    job.get("submission_error") or job.get("last_error", "")
                )):
            job["status"] = "entry_rejected"
            job["next_attempt_at"] = None
            job["rejection_code"] = _MARKET_ORDER_UNSUPPORTED
            self.ledger.save_bearish_option_job(job["job_id"], job)
            logger.warning(
                "Marked previously uncertain bearish PUT entry rejected for trade_id=%s symbol=%s contract=%s: %s",
                job.get("trade_id"),
                job.get("symbol"),
                job.get("contract_symbol"),
                job["last_error"],
            )
            return
        if not self._is_due(job):
            return
        if job["status"] == "exits_submitting":
            self._reconcile_exit_submission(job)
            return
        self._reconcile_entry(job)

    @staticmethod
    def _is_due(job: dict[str, Any]) -> bool:
        value = job.get("next_attempt_at")
        if not value:
            return True
        return datetime.fromisoformat(value) <= datetime.now(UTC)

    def _defer(self, job: dict[str, Any], exc: BaseException) -> None:
        if _is_market_order_unsupported(job.get("submission_error", "")):
            job["status"] = "entry_rejected"
            job["next_attempt_at"] = None
            job["rejection_code"] = _MARKET_ORDER_UNSUPPORTED
            job["last_reconcile_error"] = str(exc)[:500]
            self.ledger.save_bearish_option_job(job["job_id"], job)
            logger.warning(
                "Stopped bearish PUT reconciliation after confirmed market-order rejection: trade_id=%s",
                job.get("trade_id"),
            )
            return

        if _is_order_not_present(exc):
            retry_count = int(job.get("order_not_present_count", 0)) + 1
            job["order_not_present_count"] = retry_count
            job["last_reconcile_error"] = str(exc)[:500]
            if retry_count >= _MAX_ORDER_NOT_PRESENT_RETRIES:
                job["status"] = "entry_unresolved"
                job["next_attempt_at"] = None
                self.ledger.save_bearish_option_job(job["job_id"], job)
                logger.error(
                    "Stopping bearish PUT order lookups after %s 'order not present' responses; "
                    "manual Webull reconciliation required: trade_id=%s client_order_id=%s",
                    retry_count,
                    job.get("trade_id"),
                    job.get("entry_client_order_id"),
                )
                return
            job["last_error"] = str(exc)[:500]
            job["retry_count"] = retry_count
            job["next_attempt_at"] = (
                datetime.now(UTC) + timedelta(seconds=min(2 ** retry_count, 60))
            ).isoformat()
            self.ledger.save_bearish_option_job(job["job_id"], job)
            logger.warning(
                "Deferring bearish PUT reconciliation for trade_id=%s status=%s (%s/%s): %s",
                job.get("trade_id"), job["status"], retry_count,
                _MAX_ORDER_NOT_PRESENT_RETRIES, exc,
            )
            return

        retry_count = int(job.get("retry_count", 0)) + 1
        job["retry_count"] = retry_count
        job["last_reconcile_error"] = str(exc)[:500]
        if not job.get("last_error"):
            job["last_error"] = str(exc)[:500]
        job["next_attempt_at"] = (
            datetime.now(UTC) + timedelta(seconds=min(2 ** min(retry_count, 6), 60))
        ).isoformat()
        self.ledger.save_bearish_option_job(job["job_id"], job)
        logger.warning(
            "Deferring bearish PUT reconciliation for trade_id=%s status=%s: %s",
            job.get("trade_id"), job["status"], exc,
        )

    def _fetch_order(self, job: dict[str, Any], client_order_id: str) -> dict[str, Any]:
        global _next_detail_at
        with _detail_lock:
            delay = _next_detail_at - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            try:
                response = self._client().order_v3.get_order_detail(
                    job["account_id"], client_order_id,
                )
            finally:
                _next_detail_at = time.monotonic() + 2.1
        data = _response_data(response)
        matches = [
            order for order in _orders_from_response(data)
            if order.get("client_order_id") == client_order_id
        ]
        if len(matches) != 1:
            raise ValueError("Webull did not return exactly one matching order")
        return matches[0]

    def _reconcile_entry(self, job: dict[str, Any]) -> None:
        try:
            order = self._fetch_order(job, job["entry_client_order_id"])
            self._validate_entry_order(job, order)
            status = order.get("status")
            if status not in _KNOWN_STATUSES:
                raise ValueError(f"Unrecognized Webull entry status: {status}")
            filled = self._decimal(order.get("filled_quantity"), "filled_quantity")
            total = self._decimal(order.get("total_quantity"), "total_quantity")
            if total != Decimal(job["quantity"]) or filled > total:
                raise ValueError("Webull PUT entry quantity differs from persisted intent")
            if status == "FILLED" and filled != total:
                raise ValueError("Webull marked the PUT entry FILLED with an incomplete quantity")

            job["entry_order_id"] = order.get("order_id") or job.get("entry_order_id")
            job["entry_status"] = status
            job["filled_quantity"] = str(filled)

            if status == "PARTIAL_FILLED" and filled < total:
                if not job.get("cancel_requested"):
                    order_id = job.get("entry_order_id")
                    if not order_id:
                        raise ValueError("Cannot cancel partial PUT entry without Webull order_id")
                    cancel_response = self._client().order_v3.cancel_order(job["account_id"], order_id)
                    _response_data(cancel_response)
                    job["cancel_requested"] = True
                    job["status"] = "entry_cancel_pending"
                    job["next_attempt_at"] = (datetime.now(UTC) + timedelta(seconds=2)).isoformat()
                    self.ledger.save_bearish_option_job(job["job_id"], job)
                    return
                job["status"] = "entry_cancel_pending"
                job["next_attempt_at"] = (datetime.now(UTC) + timedelta(seconds=2)).isoformat()
                self.ledger.save_bearish_option_job(job["job_id"], job)
                return

            if status not in _TERMINAL_STATUSES and filled < total:
                job["status"] = "entry_pending"
                job["next_attempt_at"] = (datetime.now(UTC) + timedelta(seconds=2)).isoformat()
                self.ledger.save_bearish_option_job(job["job_id"], job)
                return

            if filled == 0:
                job["status"] = "entry_no_fill"
                job["next_attempt_at"] = None
                self.ledger.save_bearish_option_job(job["job_id"], job)
                return

            filled_price = self._decimal(order.get("filled_price"), "filled_price")
            if filled_price <= 0:
                raise ValueError("Webull returned a nonpositive PUT fill price")
            if filled != filled.to_integral_value():
                raise ValueError("Webull returned a fractional option contract fill")
            self._submit_exits(job, filled, filled_price)
        except Exception as exc:
            self._defer(job, exc)

    @staticmethod
    def _validate_entry_order(job: dict[str, Any], order: dict[str, Any]) -> None:
        if (order.get("symbol") != job["symbol"]
                or order.get("side") != "BUY"
                or order.get("instrument_type") != "OPTION"):
            raise ValueError("Webull PUT entry identity differs from persisted intent")

    @staticmethod
    def _decimal(value: Any, field: str) -> Decimal:
        if isinstance(value, bool) or value is None:
            raise ValueError(f"Webull order detail is missing {field}")
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"Webull order detail has invalid {field}") from exc
        if not parsed.is_finite() or parsed < 0:
            raise ValueError(f"Webull order detail has invalid {field}")
        return parsed

    def _submit_exits(self, job: dict[str, Any], quantity: Decimal, filled_price: Decimal) -> None:
        module = self._module()
        profit_price = Decimal(str(module.round_to_tick(
            float(filled_price * (1 + Decimal(str(job["profit_percent"])) / 100)), 0.05,
        )))
        stop_price = Decimal(str(module.round_to_tick(
            float(filled_price * (1 - Decimal(str(job["stop_loss_percent"])) / 100)), 0.05,
        )))
        if not (Decimal(0) < stop_price < filled_price < profit_price):
            raise ValueError("Rounded PUT fill-based exit prices are invalid")

        exit_orders = [{
            "client_order_id": job["profit_client_order_id"],
            "combo_type": "STOP_PROFIT",
            "option_strategy": "SINGLE",
            "instrument_type": "OPTION",
            "market": "US",
            "symbol": job["symbol"],
            "order_type": "LIMIT",
            "limit_price": f"{profit_price:.2f}",
            "quantity": str(int(quantity)),
            "side": "SELL",
            "time_in_force": "GTC",
            "entrust_type": "QTY",
            "legs": [self._option_leg(
                job["symbol"], job["strike"], job["expiration"], "SELL", int(quantity),
            )],
        }]
        if job["stop_loss_enabled"]:
            exit_orders.append({
                "client_order_id": job["stop_client_order_id"],
                "combo_type": "STOP_LOSS",
                "option_strategy": "SINGLE",
                "instrument_type": "OPTION",
                "market": "US",
                "symbol": job["symbol"],
                "order_type": "STOP_LOSS",
                "stop_price": f"{stop_price:.2f}",
                "quantity": str(int(quantity)),
                "side": "SELL",
                "time_in_force": "GTC",
                "entrust_type": "QTY",
                "legs": [self._option_leg(
                    job["symbol"], job["strike"], job["expiration"], "SELL", int(quantity),
                )],
            })

        job["fill_price"] = str(filled_price)
        job["exit_quantity"] = str(int(quantity))
        job["profit_price"] = f"{profit_price:.2f}"
        job["stop_price"] = f"{stop_price:.2f}" if job["stop_loss_enabled"] else None
        job["exit_orders"] = exit_orders
        job["status"] = "exits_submitting"
        job["next_attempt_at"] = datetime.now(UTC).isoformat()
        self.ledger.save_bearish_option_job(job["job_id"], job)

        try:
            response = self._client().order_v3.place_order(
                job["account_id"],
                exit_orders,
                client_combo_order_id=job["exit_combo_order_id"],
            )
            _response_data(response)
            job["status"] = "exits_active"
            job["next_attempt_at"] = None
            job["last_error"] = None
            self.ledger.save_bearish_option_job(job["job_id"], job)
            logger.info(
                "Broker-held bearish PUT exits submitted: trade_id=%s fill=%s profit=%s stop=%s",
                job["trade_id"], job["fill_price"], job["profit_price"], job["stop_price"],
            )
        except Exception as exc:
            self._defer(job, exc)

    def _reconcile_exit_submission(self, job: dict[str, Any]) -> None:
        try:
            order = self._fetch_order(job, job["profit_client_order_id"])
            order_ids = {order.get("client_order_id")}
            if order.get("client_order_id") != job["profit_client_order_id"]:
                raise ValueError("Webull exit detail did not match the profit order")
            if job.get("stop_client_order_id"):
                stop = self._fetch_order(job, job["stop_client_order_id"])
                order_ids.add(stop.get("client_order_id"))
            if len(order_ids) == 1 and job.get("stop_client_order_id"):
                raise ValueError("Only one of the paired PUT exits is visible at Webull")
            job["status"] = "exits_active"
            job["next_attempt_at"] = None
            job["last_error"] = None
            self.ledger.save_bearish_option_job(job["job_id"], job)
        except Exception as exc:
            self._defer(job, exc)


def _reconcile_worker(database_path: str) -> None:
    ledger = Ledger(database_path)
    while True:
        try:
            if ledger.bearish_option_jobs():
                with ledger.bearish_option_worker_lock() as acquired:
                    if acquired:
                        lifecycle = BearishPutLifecycle(ledger)
                        for job in ledger.bearish_option_jobs():
                            lifecycle.reconcile(job)
                            time.sleep(2.1)
        except Exception:
            logger.exception("Bearish PUT fill reconciler failed")
        time.sleep(1.0)


def start_bearish_put_reconciler(database_path: str) -> None:
    path = str(database_path)
    with _worker_guard:
        if path in _worker_paths:
            return
        _worker_paths.add(path)
    threading.Thread(
        target=_reconcile_worker,
        args=(path,),
        name="bearish-put-reconciler",
        daemon=True,
    ).start()