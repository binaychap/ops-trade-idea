from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.config.strategy import exit_percentages, options_margin_account_id, bullish_stock_account_id


def _load_webull_stock_module() -> Any:
    from importlib import import_module
    return import_module('app.bullish.stock_bracket')


def _load_webull_option_module() -> Any:
    from importlib import import_module
    return import_module('app.options.brackets')


def _is_webull_rate_limit_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return "429" in message or "too_many_requests" in message or "too many requests" in message or "rate limit" in message


def submit_paper_order(decision: Any, settings: Any, fingerprint: str, payload: Any | None = None) -> dict[str, Any]:
    # Accept either a dict decision or an object with model_dump / attributes
    if hasattr(decision, "model_dump"):
        d = decision.model_dump(mode="json")
    elif isinstance(decision, dict):
        d = decision
    else:
        # fallback: try attribute access
        d = {
            "action": getattr(decision, "action", "skip"),
            "symbol": getattr(decision, "symbol", ""),
            "notional_usd": getattr(decision, "notional_usd", 0.0),
        }

    if d.get("action") == "skip":
        return {"skipped": True, "reason": d.get("rationale", "Decision skipped")}
    if payload is not None and getattr(payload, "direction", None) == "neutral":
        if d.get("action") != "buy" or d.get("strategy") != "iron_condor":
            return {"skipped": True, "reason": "Neutral execution requires an iron_condor decision"}

    is_stock = d.get("action") == "buy" and not (payload is not None and getattr(payload, "direction", None) == "neutral")
    if is_stock:
        if settings.max_notional_usd < 100:
            return {"skipped": True, "reason": "$100 stock buy exceeds MAX_NOTIONAL_USD"}
        d = {**d, "notional_usd": 100.0}

    client_order_id = f"om-{fingerprint[:24]}"
    if settings.dry_run:
        return {
            "dry_run": True,
            "client_order_id": client_order_id,
            "symbol": d.get("symbol"),
            "action": d.get("action"),
            "notional_usd": d.get("notional_usd"),
            **({"order_type": "MARKET", "time_in_force": "DAY", "quantity": None} if is_stock else {}),
        }

    action = d.get("action")
    symbol = d.get("symbol")
    reference_level = None
    if payload is not None:
        reference_level = payload.entry_price or payload.target_price or payload.stop_price
    if reference_level is None:
        reference_level = max(float(d.get("notional_usd") or 0.0) / 100.0, 1.0)

    if action == "sell_short":
        from app.broker.quotes import QuoteError
        profit_percent, stop_loss_percent = exit_percentages(settings, "bearish")
        from app.bearish.executor import BearishPutOptionExecutor
        option_module = _load_webull_option_module()
        executor = BearishPutOptionExecutor(module=option_module)
        expiry = None  # Resolve the earliest future listed PUT expiration from the chain.
        strike = round(float(reference_level) / 5.0) * 5.0
        try:
            order_result = executor.submit(
                account_id=options_margin_account_id(option_module, settings),
                symbol=symbol,
                strike=strike,
                expiration=expiry,
                quantity=1,
                profit_percent=profit_percent,
                stop_loss_percent=stop_loss_percent,
                quote_max_age_seconds=getattr(settings, "bearish_quote_max_age_seconds", 60),
            )
        except QuoteError as exc:
            import logging
            logging.getLogger(__name__).warning("Skipping bearish PUT for %s: %s", symbol, exc)
            return {"skipped": True, "reason": str(exc)}
        except Exception as exc:
            # If no option contracts are available, treat this idea as skipped.
            msg = str(exc)
            if "No option contracts found" in msg or "Contract validation failed" in msg:
                return {"skipped": True, "reason": msg}
            raise
        return {
            "dry_run": False,
            "id": str(order_result.get("order_id") or order_result.get("client_order_id") or ""),
            "client_order_id": client_order_id,
            "symbol": symbol,
            "status": "submitted",
            "side": "SELL",
            "notional_usd": d.get("notional_usd"),
            "broker": "webull",
            "option": {"type": executor.option_type(), "strategy": executor.strategy_label()},
        }

    if payload is not None and getattr(payload, "direction", None) == "neutral":
        from app.ironcondor.executor import IronCondorOptionExecutor, CondorValidationError
        from app.persistence.ledger import Ledger
        from app.broker.quotes import QuoteError

        ledger = Ledger(settings.database_path)
        reservation = f"iron-condor:{fingerprint}"
        attempted = False

        class AlreadySubmitted(Exception):
            pass

        def persist_before_submit(plan):
            nonlocal attempted
            # Atomic insert precedes the network call; persisted IDs survive a crash.
            if not ledger.reserve(reservation, plan):
                raise AlreadySubmitted()
            attempted = True
            ledger.finish(reservation, "submitting", d, plan)

        try:
            option_module = _load_webull_option_module()
            executor = IronCondorOptionExecutor(module=option_module)
            expiry = (datetime.now(UTC) + timedelta(days=30)).strftime("%Y-%m-%d")
            profit_percent, stop_loss_percent = exit_percentages(settings, "iron_condor")
            result = executor.submit(
                account_id=options_margin_account_id(option_module, settings), symbol=symbol,
                expiration=expiry, reference_price=payload.entry_price,
                max_risk_usd=min(float(d.get("notional_usd") or 0), settings.max_notional_usd),
                exit_time_in_force="GTC", quantity=1, before_submit=persist_before_submit,
                profit_percent=profit_percent, stop_loss_percent=stop_loss_percent,
                quote_max_age_seconds=getattr(settings, "iron_condor_quote_max_age_seconds", 60),
            )
        except AlreadySubmitted:
            return {"skipped": True, "reason": "Iron-condor attempt already recorded; reconcile saved event before retrying"}
        except (CondorValidationError, QuoteError) as exc:
            if attempted:
                ledger.fail(reservation, type(exc).__name__)
                raise
            return {"skipped": True, "reason": str(exc)}
        except Exception as exc:
            if attempted:
                ledger.fail(reservation, f"{type(exc).__name__}: submission unresolved; reconcile saved order IDs")
            raise
        ledger.finish(reservation, "ordered", d, result)
        return {
            "dry_run": False, "id": result["entry_id"],
            "client_order_id": result["entry_id"], "symbol": symbol,
            "status": "submitted", "side": "SELL", "broker": "webull",
            "notional_usd": d.get("notional_usd"),
            "option": {"type": "IRON_CONDOR", "strategy": "iron_condor"},
            **{key: result[key] for key in ("combo_id", "entry_id", "profit_id", "stop_id",
                                           "entry_credit", "profit_debit", "stop_debit", "max_loss_usd")},
        }

    webull_module = _load_webull_stock_module()
    account_id = bullish_stock_account_id(webull_module, settings)

    order_kwargs = dict(account_id=account_id, symbol=symbol)

    if getattr(settings, "next_day_exit_enabled", False):
        if action != "buy":
            raise ValueError("Next-day stock exits support long buy entries only")
        from app.persistence.ledger import Ledger
        from app.broker.stocks import StockExecution

        ledger = Ledger(settings.database_path)
        if ledger.has_active_exit_job(account_id=account_id, symbol=symbol):
            raise RuntimeError(f"Duplicate active stock exit job already exists for {symbol} on account {account_id}")

        with ledger.exit_worker_lock() as acquired:
            if not acquired:
                raise RuntimeError("Stock exit worker busy; entry not submitted")
            broker = StockExecution(webull_module.get_trade_client())
            if broker.position(account_id, symbol) != 0:
                raise ValueError("Scheduled entry requires no existing stock position for this symbol")

            tracked_job = None

            def record_intent(tracking):
                nonlocal tracked_job
                job = {
                    "id": client_order_id, "account_id": account_id,
                    "symbol": symbol, "quantity": None, "entry_kind": "amount",
                    **tracking, "status": "waiting_entry", "market_orders": [],
                    "due_at": None, "last_error": None,
                }
                ledger.register_exit_job(job)
                tracked_job = job

            try:
                order_result = webull_module.buy_stock(
                    **order_kwargs, exit_time_in_force="GTC", before_submit=record_intent,
                )
            except Exception as exc:
                if tracked_job is not None:
                    tracked_job["entry_submission_error"] = str(exc)[:500]
                    tracked_job["last_error"] = "Entry submission was not confirmed. Broker reconciliation is required."
                    ledger.save_exit_job(tracked_job)
                raise
    else:
        order_result = webull_module.buy_stock(**order_kwargs)

    return {
        "dry_run": False,
        "id": str(order_result.get("entry_id") or order_result.get("order_id") or order_result.get("client_order_id") or ""),
        "client_order_id": client_order_id,
        "symbol": symbol,
        "status": "submitted",
        "side": "BUY" if action == "buy" else "SELL",
        "notional_usd": d.get("notional_usd"),
        "broker": "webull",
        "bracket": {key: order_result.get(key) for key in ("combo_id", "entry_id", "profit_id", "stop_id")},
    }


__all__ = ["submit_paper_order", "_is_webull_rate_limit_error"]
