"""Bearer-authenticated trading API for the iOS companion client.

All routes live under /api/trading and require
``Authorization: Bearer <IOS_API_KEY>``. When IOS_API_KEY is unset every
route returns 503.

- GET  /account         account balances plus equity positions
- GET  /orders          recent broker orders (ledger fallback)
- POST /orders/preview  validate a manual stock order; never submits
- POST /orders          re-validate and submit (confirm=true required)

Manual BUY orders request $100 at MARKET / DAY without attached exits. Manual
SELL orders are single-leg NORMAL orders placed through the same
order_v3.place_order call pattern the scheduled-exit market sells use.
Stocks only; no option order support.
"""
from __future__ import annotations

import hmac
import json
import logging
import math
import re
import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from app.persistence.ledger import Ledger
from app.config.strategy import bullish_stock_account_id
from app.broker.client import get_trade_client
from app.broker.quotes import QuoteError, current_stock_quote

logger = logging.getLogger("optionomics_bot")

router = APIRouter(prefix="/api/trading")

SYMBOL_RE = re.compile(r"[A-Z][A-Z0-9.]{0,9}")
MANUAL_PREFIX = "manual:"
IOS_CLIENT_PREFIX = "ios-"
# Sandbox snapshots can lag ~15 minutes; tolerate that for reference pricing.
QUOTE_MAX_AGE_SECONDS = 1200
# Checks that must pass before POST /orders will submit.
BLOCKING_CHECKS = frozenset({"symbol_valid", "quantity_valid", "price_valid", "notional_cap"})


# ---------------------------------------------------------------------------
# Settings / auth (get_settings is imported lazily to avoid a main.py cycle)
# ---------------------------------------------------------------------------

def _get_settings() -> Any:
    from app.main import get_settings

    return get_settings()


def require_trading_auth(authorization: str | None = Header(default=None)) -> None:
    settings = _get_settings()
    key = str(settings.ios_api_key or "").strip()
    if not key:
        raise HTTPException(status_code=503, detail="trading API disabled: set IOS_API_KEY")
    token = ""
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer":
            token = value.strip()
    try:
        ok = bool(token) and hmac.compare_digest(token, key)
    except (TypeError, ValueError):
        ok = False
    if not ok:
        raise HTTPException(status_code=401, detail="unauthorized")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if isinstance(value, bool):
            return default
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _pick(mapping: Any, keys: list[str], default: Any = None) -> Any:
    if not isinstance(mapping, dict):
        return default
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return default


def _checked_json(response: Any) -> Any:
    if getattr(response, "status_code", None) != 200:
        raise RuntimeError(f"Webull request failed: HTTP {getattr(response, 'status_code', '?')}")
    data = response.json()
    if isinstance(data, dict) and data.get("error_code"):
        raise RuntimeError(f"Webull request failed: {data.get('error_code')}")
    return data


def _parse_time(value: Any) -> str | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        if isinstance(value, (int, float)):
            ts = float(value)
            if ts > 1e12:  # milliseconds
                ts /= 1000.0
            stamp = datetime.fromtimestamp(ts, UTC)
        else:
            text = str(value).strip()
            if not text:
                return None
            if text.isdigit():
                return _parse_time(int(text))
            stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=UTC)
            stamp = stamp.astimezone(UTC)
        return stamp.isoformat(timespec="seconds").replace("+00:00", "Z")
    except (ValueError, OverflowError, OSError):
        return str(value)


def _market_open() -> bool:
    from app.main import is_market_open_et

    try:
        return bool(is_market_open_et())
    except Exception:
        return False


def _load_stock_module() -> Any:
    from importlib import import_module
    return import_module('app.bullish.stock_bracket')


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------

def _resolve_account() -> tuple[str, str]:
    """Return (account_id, account_number) for the stock trading account."""
    settings = _get_settings()
    accounts = _checked_json(get_trade_client().account_v2.get_account_list())
    if isinstance(accounts, dict):
        accounts = accounts.get("accounts", [])
    if not isinstance(accounts, list) or not accounts:
        raise RuntimeError("No Webull account found")
    configured = str(settings.bullish_stock_account_number or "").strip()
    if configured:
        matches = [
            a for a in accounts
            if isinstance(a, dict) and str(a.get("account_number") or "").strip() == configured
        ]
        if len(matches) != 1 or not matches[0].get("account_id"):
            raise ValueError("Configured BULLISH_STOCK_ACCOUNT_NUMBER must match exactly one available account")
        account = matches[0]
    else:
        account = accounts[0]
    if not isinstance(account, dict) or not account.get("account_id"):
        raise RuntimeError("No Webull account found")
    return str(account["account_id"]), str(account.get("account_number") or "")


def _fetch_position_rows(account_id: str) -> list[dict[str, Any]]:
    rows = _checked_json(get_trade_client().account_v2.get_account_position(account_id))
    if isinstance(rows, dict):
        rows = rows.get("positions", [])
    return [row for row in (rows or []) if isinstance(row, dict)]


def _map_position(row: dict[str, Any]) -> dict[str, Any] | None:
    if row.get("instrument_type") not in (None, "EQUITY"):
        return None
    symbol = str(row.get("symbol") or "").strip().upper()
    if not symbol:
        return None
    quantity = _num(row.get("quantity"))
    avg_cost = _num(_pick(row, ["avg_cost", "average_cost", "cost_price", "avg_price"]))
    market_price = _num(_pick(row, ["market_price", "last_price", "price", "current_price", "close_price"]))
    market_value = _num(_pick(row, ["market_value", "position_value"]), quantity * market_price)
    unrealized_pnl = _num(_pick(row, ["unrealized_pnl", "unrealized_profit_loss", "pnl", "profit_loss"]))
    unrealized_pnl_pct = _num(_pick(row, ["unrealized_pnl_pct", "unrealized_profit_loss_rate", "pnl_rate"]))
    if not unrealized_pnl_pct and avg_cost and quantity:
        unrealized_pnl_pct = unrealized_pnl / (avg_cost * quantity) * 100.0
    return {
        "symbol": symbol,
        "quantity": quantity,
        "avg_cost": avg_cost,
        "market_price": market_price,
        "market_value": market_value,
        "unrealized_pnl": unrealized_pnl,
        "unrealized_pnl_pct": unrealized_pnl_pct,
    }


@router.get("/account", dependencies=[Depends(require_trading_auth)])
def trading_account() -> dict[str, Any]:
    try:
        account_id, account_number = _resolve_account()
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from None
    except Exception as exc:
        logger.warning("Trading API: account lookup failed: %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail=f"broker request failed: {exc}") from None
    try:
        balance = _checked_json(get_trade_client().account_v2.get_account_balance(account_id))
        position_rows = _fetch_position_rows(account_id)
    except Exception as exc:
        logger.warning("Trading API: account snapshot failed: %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail=f"broker request failed: {exc}") from None
    if not isinstance(balance, dict):
        balance = {}
    positions = [mapped for row in position_rows if (mapped := _map_position(row)) is not None]
    return {
        "account_number": account_number,
        "currency": str(_pick(balance, ["currency"], "USD") or "USD"),
        "total_value": _num(_pick(balance, ["total_value", "total_account_value", "net_liquidation", "account_value", "total_assets"])),
        "cash": _num(_pick(balance, ["cash", "cash_balance", "total_cash", "available_cash", "settled_cash"])),
        "buying_power": _num(_pick(balance, ["buying_power", "day_trading_buying_power", "available_funds", "buying_power_value"])),
        "positions": positions,
        "as_of": _utc_now_iso(),
    }


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

def _map_broker_order(row: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    symbol = str(row.get("symbol") or "").strip().upper()
    if not symbol:
        return None
    side_raw = str(row.get("side") or "").strip().upper()
    side = {"BUY": "buy", "SELL": "sell"}.get(side_raw, side_raw.lower() or "buy")
    type_raw = str(row.get("order_type") or "").strip().upper()
    order_type = {"MARKET": "market", "LIMIT": "limit"}.get(type_raw, type_raw.lower() or "market")
    client_order_id = str(row.get("client_order_id") or "")
    limit_raw = row.get("limit_price")
    return {
        "order_id": str(_pick(row, ["order_id", "id", "broker_order_id"]) or client_order_id),
        "client_order_id": client_order_id,
        "symbol": symbol,
        "side": side,
        "order_type": order_type,
        "quantity": _num(_pick(row, ["total_quantity", "quantity", "order_quantity"])),
        "limit_price": _num(limit_raw) if limit_raw not in (None, "") else None,
        "status": str(row.get("status") or "unknown"),
        "filled_quantity": _num(row.get("filled_quantity")) if row.get("filled_quantity") not in (None, "") else None,
        "created_at": _parse_time(_pick(row, ["create_time", "created_at", "create_time_at", "order_time", "placed_time"])),
        "source": "manual" if client_order_id.startswith(IOS_CLIENT_PREFIX) else "bot",
    }


def _ledger_manual_orders(database_path: str, limit: int) -> list[dict[str, Any]]:
    """Fallback order list from local manual-order events when the broker is unreachable."""
    path = Path(database_path).resolve()
    if not path.exists():
        return []
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)) as conn:
        conn.row_factory = sqlite3.Row
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "events" not in tables:
            return []
        rows = conn.execute(
            "SELECT fingerprint, status, payload_json, order_json, created_at FROM events "
            "WHERE fingerprint LIKE 'manual:%' ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    orders: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except ValueError:
            payload = {}
        try:
            order = json.loads(row["order_json"] or "{}")
        except ValueError:
            order = {}
        if not isinstance(payload, dict):
            payload = {}
        if not isinstance(order, dict):
            order = {}
        client_order_id = str(row["fingerprint"])[len(MANUAL_PREFIX):]
        orders.append({
            "order_id": str(order.get("order_id") or ""),
            "client_order_id": client_order_id,
            "symbol": str(payload.get("symbol") or "").upper(),
            "side": str(payload.get("side") or ""),
            "order_type": str(payload.get("order_type") or ""),
            "quantity": _num(payload.get("quantity")),
            "limit_price": payload.get("limit_price"),
            "status": str(order.get("status") or row["status"]),
            "filled_quantity": None,
            "created_at": row["created_at"],
            "source": str(payload.get("source") or "manual"),
        })
    return orders


@router.get("/orders", dependencies=[Depends(require_trading_auth)])
def trading_orders(limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
    settings = _get_settings()
    try:
        account_id, _ = _resolve_account()
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from None
    except Exception as exc:
        logger.warning("Trading API: account lookup failed: %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail=f"broker request failed: {exc}") from None
    try:
        history = _checked_json(
            get_trade_client().order_v3.get_order_history(account_id, page_size=min(limit, 100))
        )
        rows = history.get("orders") if isinstance(history, dict) else history
        orders = [
            mapped for row in (rows or [])
            if (mapped := _map_broker_order(row)) is not None
        ][:limit]
    except Exception:
        logger.warning("Trading API: order history failed; falling back to ledger")
        orders = _ledger_manual_orders(settings.database_path, limit)
    return {"orders": orders, "as_of": _utc_now_iso()}


# ---------------------------------------------------------------------------
# Order preview / submission
# ---------------------------------------------------------------------------

class OrderTicket(BaseModel):
    model_config = ConfigDict(extra="ignore")

    symbol: str
    side: str
    order_type: str
    quantity: float = 1
    limit_price: float | None = None


class SubmitTicket(OrderTicket):
    confirm: bool = False


def _preview_ticket(ticket: OrderTicket) -> dict[str, Any]:
    settings = _get_settings()
    checks: list[dict[str, Any]] = []
    warnings: list[str] = []

    side = str(ticket.side or "").strip().lower()
    if side not in ("buy", "sell"):
        raise HTTPException(status_code=422, detail=f"side must be 'buy' or 'sell', got {ticket.side!r}")
    order_type = str(ticket.order_type or "").strip().lower()
    if order_type not in ("market", "limit"):
        raise HTTPException(status_code=422, detail=f"order_type must be 'market' or 'limit', got {ticket.order_type!r}")

    symbol = str(ticket.symbol or "").strip().upper()
    symbol_ok = bool(SYMBOL_RE.fullmatch(symbol))
    checks.append({
        "name": "symbol_valid", "passed": symbol_ok,
        "detail": "symbol looks like a valid ticker" if symbol_ok else f"invalid symbol: {ticket.symbol!r}",
    })

    if side == "buy":
        order_type = "market"
        warnings.append("Stock buys use $100 MARKET / DAY without attached exits; quantity and limit price are ignored")
    quantity = ticket.quantity if side == "sell" else 1
    quantity_ok = math.isfinite(quantity) and quantity > 0
    checks.append({
        "name": "quantity_valid", "passed": quantity_ok,
        "detail": "quantity is a positive number" if quantity_ok else f"quantity must be a positive number, got {quantity!r}",
    })

    if side == "sell" and quantity_ok and not float(quantity).is_integer() and order_type != "market":
        checks.append({"name": "price_valid", "passed": False,
                       "detail": "fractional sells require a market order"})

    limit_price = ticket.limit_price
    if isinstance(limit_price, bool):
        limit_price = None
    if order_type == "limit":
        price_ok = (
            limit_price is not None
            and math.isfinite(float(limit_price))
            and float(limit_price) > 0
        )
        checks.append({
            "name": "price_valid", "passed": price_ok,
            "detail": "limit price is positive" if price_ok else "limit orders require a positive limit_price",
        })
    else:
        price_ok = True
        limit_price = None
        checks.append({"name": "price_valid", "passed": True, "detail": "market order needs no limit price"})

    reference_price: float | None = None
    if symbol_ok:
        try:
            reference_price = float(current_stock_quote(symbol, max_age_seconds=QUOTE_MAX_AGE_SECONDS)["price"])
        except QuoteError as exc:
            warnings.append(f"reference quote unavailable: {exc}")
        except Exception as exc:
            warnings.append(f"reference quote unavailable: {type(exc).__name__}")

    price_basis = float(limit_price) if (order_type == "limit" and price_ok) else reference_price
    estimated_notional = round(quantity * price_basis, 2) if (quantity_ok and price_basis) else None
    if side == "buy":
        estimated_notional = 100.0
        from app.bullish.stock_bracket import validate_stock_buy
        try:
            validate_stock_buy(reference_price or 0, settings.max_notional_usd)
        except ValueError as exc:
            checks.append({"name": "price_valid", "passed": False, "detail": str(exc)})
    max_notional = float(settings.max_notional_usd)
    if estimated_notional is None:
        notional_ok = False
        notional_detail = "cannot estimate notional without a reference price"
    else:
        notional_ok = estimated_notional <= max_notional
        notional_detail = (
            f"estimated ${estimated_notional:,.2f} is within the ${max_notional:,.2f} cap"
            if notional_ok
            else f"estimated ${estimated_notional:,.2f} exceeds the ${max_notional:,.2f} cap"
        )
    checks.append({"name": "notional_cap", "passed": notional_ok, "detail": notional_detail})

    market_open = _market_open()
    if not market_open:
        warnings.append("market is closed (ET); the order may rest until the next session")

    if side == "sell" and symbol_ok and quantity_ok:
        try:
            account_id, _ = _resolve_account()
            held = sum(
                _num(row.get("quantity"))
                for row in _fetch_position_rows(account_id)
                if str(row.get("symbol") or "").strip().upper() == symbol
                and row.get("instrument_type") in (None, "EQUITY")
            )
            position_ok = held >= quantity
            checks.append({
                "name": "position_check", "passed": position_ok,
                "detail": f"holding {held:g} shares of {symbol}" if position_ok
                else f"selling {quantity} shares but holding {held:g} of {symbol}",
            })
            if not position_ok:
                warnings.append("sell quantity exceeds the current position")
        except Exception as exc:
            warnings.append(f"position check unavailable: {type(exc).__name__}")

    return {
        "ok": True,
        "symbol": symbol,
        "side": side,
        "order_type": order_type,
        "quantity": None if side == "buy" else quantity,
        "notional_usd": 100.0 if side == "buy" else None,
        "time_in_force": "DAY" if order_type == "market" else "GTC",
        "limit_price": round(float(limit_price), 2) if (order_type == "limit" and price_ok) else None,
        "reference_price": reference_price,
        "estimated_notional": estimated_notional,
        "max_notional_usd": max_notional,
        "within_notional_cap": notional_ok,
        "market_open": market_open,
        "dry_run": bool(settings.dry_run),
        "checks": checks,
        "warnings": warnings,
    }


def _blocking_failures(preview: dict[str, Any]) -> list[str]:
    return [
        str(check.get("detail") or check.get("name"))
        for check in preview.get("checks", [])
        if check.get("name") in BLOCKING_CHECKS and not check.get("passed")
    ]


def _submit_stock_order(preview: dict[str, Any], client_order_id: str, settings: Any) -> dict[str, Any]:
    symbol = preview["symbol"]
    side = preview["side"]
    order_type = preview["order_type"]
    quantity = preview["quantity"]
    limit_price = preview["limit_price"]
    trade_client = get_trade_client()
    module = _load_stock_module()
    account_id = bullish_stock_account_id(module, settings)

    if side == "buy":
        if settings.max_notional_usd < 100:
            raise ValueError("$100 stock buy exceeds MAX_NOTIONAL_USD")
        result = module.buy_stock(account_id=account_id, symbol=symbol,
                                  client_order_id=client_order_id, trade_client=trade_client)
        if not isinstance(result, dict):
            raise RuntimeError("Unexpected stock order response")
        order_id = str(result.get("order_id") or result.get("entry_id") or "")
        return {
            "order_id": order_id,
            "extra": {
                "broker": "webull",
                "bracket": {key: result.get(key) for key in ("combo_id", "entry_id", "profit_id", "stop_id")},
            },
        }

    # SELL: single-leg NORMAL order, same leg shape the scheduled-exit
    # market sells use in app/broker/stocks.py.
    leg: dict[str, Any] = {
        "client_order_id": client_order_id,
        "combo_type": "NORMAL",
        "symbol": symbol,
        "instrument_type": "EQUITY",
        "market": "US",
        "side": "SELL",
        "order_type": "MARKET" if order_type == "market" else "LIMIT",
        "quantity": str(quantity),
        "time_in_force": "DAY" if order_type == "market" else "GTC",
        "support_trading_session": "CORE",
        "entrust_type": "QTY",
    }
    if order_type == "limit":
        leg["limit_price"] = f"{float(limit_price):.2f}"
    result = _checked_json(trade_client.order_v3.place_order(account_id, [leg]))
    if not isinstance(result, dict):
        raise RuntimeError("Unexpected stock order response")
    order_id = str(result.get("order_id") or result.get("client_order_id") or client_order_id)
    return {"order_id": order_id, "extra": {"broker": "webull"}}


@router.post("/orders/preview", dependencies=[Depends(require_trading_auth)])
def preview_order(ticket: OrderTicket) -> dict[str, Any]:
    return _preview_ticket(ticket)


@router.post("/orders", dependencies=[Depends(require_trading_auth)])
def submit_order(ticket: SubmitTicket) -> dict[str, Any]:
    if not ticket.confirm:
        raise HTTPException(status_code=422, detail="confirmation required")
    preview = _preview_ticket(ticket)
    failures = _blocking_failures(preview)
    if failures:
        raise HTTPException(status_code=422, detail="; ".join(failures))

    settings = _get_settings()
    client_order_id = f"{IOS_CLIENT_PREFIX}{uuid.uuid4().hex[:28]}"  # Webull max: 32 chars
    ledger = Ledger(settings.database_path)
    record = {
        "source": "manual",
        "symbol": preview["symbol"],
        "side": preview["side"],
        "order_type": preview["order_type"],
        "quantity": preview["quantity"],
        "limit_price": preview["limit_price"],
        "client_order_id": client_order_id,
        "preview": preview,
    }
    fingerprint = f"{MANUAL_PREFIX}{client_order_id}"
    if not ledger.reserve(fingerprint, record):
        raise HTTPException(status_code=409, detail="duplicate order submission")

    if settings.dry_run:
        order_payload = {"dry_run": True, "client_order_id": client_order_id, "status": "dry_run"}
        ledger.finish(fingerprint, "dry_run", record, order_payload)
        return {
            "dry_run": True,
            "status": "dry_run",
            "order_id": None,
            "client_order_id": client_order_id,
            "preview": preview,
            "message": "DRY_RUN=true: order validated but not submitted",
        }

    try:
        result = _submit_stock_order(preview, client_order_id, settings)
    except Exception as exc:
        logger.warning("Trading API: manual order submission failed: %s", type(exc).__name__)
        ledger.fail(fingerprint, f"{type(exc).__name__}: {exc}"[:1000])
        raise HTTPException(status_code=502, detail=f"broker submission failed: {exc}") from None

    order_payload = {
        "order_id": result["order_id"],
        "client_order_id": client_order_id,
        "status": "submitted",
        **result.get("extra", {}),
    }
    ledger.finish(fingerprint, "submitted", record, order_payload)
    return {
        "dry_run": False,
        "status": "submitted",
        "order_id": result["order_id"],
        "client_order_id": client_order_id,
        "preview": preview,
    }
