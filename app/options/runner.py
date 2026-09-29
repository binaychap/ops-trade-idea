from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal
from enum import Enum
from zoneinfo import ZoneInfo
import sys

from app.feeds.optionomics_client import fetch_trade_ideas

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import SettingsConfigDict
from app.config.webull import resolve_webull_endpoint
from app.config.strategy import StrategyExitSettings, exit_percentages, options_margin_account_id
from webull.core.client import ApiClient
from webull.data.data_client import DataClient

logger = logging.getLogger("optionomics_bot")
logger.setLevel(logging.INFO)
logger.propagate = False
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)

# Keep the app at INFO while suppressing noisy background watchers and debug logs.
logging.getLogger().setLevel(logging.INFO)
for _name in ("debugpy", "pydevd", "asyncio", "urllib3", "webull", "uvicorn.access", "uvicorn.error", "watchfiles", "watchfiles.main"):
    logging.getLogger(_name).setLevel(logging.WARNING if _name.startswith("watchfiles") else logging.INFO)


def color_symbol(symbol: str | None) -> str:
    value = str(symbol or "unknown").upper()
    return f"\033[32m{value}\033[0m"


def color_error(value: str | None) -> str:
    text = str(value or "unknown").upper()
    return f"\033[31m{text}\033[0m"


class Settings(StrategyExitSettings):
    """Runtime settings loaded from environment variables."""
    dry_run: bool = Field(default=True, alias="DRY_RUN")
    max_notional_usd: float = Field(default=250.0, gt=0.0, alias="MAX_NOTIONAL_USD")
    allow_short_selling: bool = Field(default=False, alias="ALLOW_SHORT_SELLING")
    force_reprocess: bool = Field(default=False, alias="FORCE_REPROCESS")
    optionomics_api_key: str | None = Field(default=None, alias="OPTIONOMICS_API_KEY")
    optionomics_email: str | None = Field(default=None, alias="OPTIONOMICS_EMAIL")
    optionomics_api_url: str = Field(default="https://optionomics.ai/api/v1/trade_ideas", alias="OPTIONOMICS_API_URL")
    optionomics_poll_enabled: bool = Field(default=True, alias="OPTIONOMICS_POLL_ENABLED")
    optionomics_poll_interval_seconds: int = Field(default=600, ge=1, alias="OPTIONOMICS_POLL_INTERVAL_SECONDS")

    webull_app_key: str | None = Field(default=None, alias="WEBULL_APP_KEY")
    webull_app_secret: str | None = Field(default=None, alias="WEBULL_APP_SECRET")

    database_path: str = Field(default="bot.sqlite3", alias="DATABASE_PATH")

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


class OrderSide(Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(Enum):
    MARKET = "MARKET"


class TimeInForce(Enum):
    DAY = "DAY"
    GTC = "GTC"


class OrderClass(Enum):
    SIMPLE = "SIMPLE"


class TradeIdea(BaseModel):
    """Validated Optionomics Trade Idea payload."""

    alert_name: str
    source: Literal["trade_idea"]
    symbol: str
    direction: Literal["bullish", "bearish", "neutral"]
    strategy: str
    model: str | None = None
    entry_price: float | None = None
    target_price: float | None = None
    stop_price: float | None = None
    triggered_at: datetime
    matched_criteria: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="ignore")

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        symbol = value.strip().upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9.]{0,9}", symbol):
            raise ValueError("symbol must be a valid ticker")

        return symbol


class TradingDecision(BaseModel):
    """Structured trade decision used by deterministic risk gates."""

    action: Literal["skip", "buy", "sell_short"]
    symbol: str
    strategy: str = "placeholder"
    notional_usd: float = Field(ge=0.0)
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str
    risk_notes: list[str] = Field(default_factory=list)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.strip().upper()


class OptionomicsTradeIdea(BaseModel):
    """Normalized Optionomics trade-idea payload consumed from the API."""

    id: str | None = None
    symbol: str
    direction: Literal["bullish", "bearish", "neutral"]
    strategy: str | None = None
    pipeline_short_name: str | None = None
    pipeline_name: str | None = None
    levels: dict[str, float] = Field(default_factory=dict)
    generated_at: datetime | None = None
    confidence_score: float | None = None
    thesis: str | None = None
    status: str | None = None

    model_config = ConfigDict(extra="ignore")

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        symbol = value.strip().upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9.]{0,9}", symbol):
            raise ValueError("symbol must be a valid ticker")
        return symbol

    @field_validator("levels")
    @classmethod
    def normalize_levels(cls, value: dict[str, Any]) -> dict[str, float]:
        normalized: dict[str, float] = {}
        for key, raw in value.items():
            try:
                normalized[key] = float(raw)
            except (TypeError, ValueError):
                continue
        return normalized


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def is_market_open_et(now: datetime | None = None) -> bool:
    current = (now or datetime.now(UTC)).astimezone(ZoneInfo("America/New_York"))
    if current.weekday() >= 5:
        return False
    market_open = current.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = current.replace(hour=16, minute=0, second=0, microsecond=0)
    return market_open <= current < market_close


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_expiration(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    if len(raw) == 8 and raw.isdigit():
        try:
            return datetime.strptime(raw, "%Y%m%d").strftime("%Y-%m-%d")
        except ValueError:
            pass
    return raw


def _flatten_option_chain(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if isinstance(raw, list):
        items: list[dict[str, Any]] = []
        for entry in raw:
            if isinstance(entry, dict):
                items.append(entry)
            elif isinstance(entry, list):
                items.extend(_flatten_option_chain(entry))
        return items
    if isinstance(raw, dict):
        items: list[dict[str, Any]] = []
        for key in ("data", "items", "contracts", "options", "result", "results"):
            value = raw.get(key)
            if value is not None:
                items.extend(_flatten_option_chain(value))
        if not items and all(key not in raw for key in ("data", "items", "contracts", "options", "result", "results")):
            return [raw]
        return items
    return []


def select_valid_webull_option_contract(
    chain: Any,
    *,
    symbol: str,
    option_type: str = "CALL",
    expiration: str | None = None,
    target_strike: float | None = None,
) -> dict[str, Any] | None:
    request_symbol = str(symbol or "").strip().upper()
    request_type = str(option_type or "").upper()
    request_expiration = _normalize_expiration(expiration)
    target = _coerce_float(target_strike)
    exact_candidates: list[tuple[float, float, dict[str, Any]]] = []
    fallback_candidates: list[tuple[float, float, float, dict[str, Any]]] = []

    requested_date = datetime.strptime(request_expiration, "%Y-%m-%d") if request_expiration else None

    for item in _flatten_option_chain(chain):
        if not isinstance(item, dict):
            continue

        underlying_symbol = str(
            item.get("underlying_symbol")
            or item.get("root_symbol")
            or item.get("underlyingSymbol")
            or item.get("rootSymbol")
            or ""
        ).upper()
        candidate_symbol = str(item.get("symbol") or item.get("ticker") or item.get("option_symbol") or "").upper()
        if request_symbol:
            symbol_matches = (
                (underlying_symbol and underlying_symbol == request_symbol)
                or (candidate_symbol and candidate_symbol == request_symbol)
                or (candidate_symbol and candidate_symbol.startswith(request_symbol))
            )
            if not symbol_matches:
                continue

        picked_type = str(
            item.get("option_type")
            or item.get("type")
            or item.get("contract_type")
            or item.get("optionType")
            or ""
        ).upper()
        if picked_type in {"CALL_OPTION", "CALL"}:
            picked_type = "CALL"
        elif picked_type in {"PUT_OPTION", "PUT"}:
            picked_type = "PUT"

        if request_type and picked_type and picked_type != request_type:
            continue

        item_expiration = _normalize_expiration(
            item.get("expiration_date")
            or item.get("expiration")
            or item.get("exp_date")
            or item.get("expire_date")
            or item.get("expiry")
        )

        strike_price = _coerce_float(
            item.get("strike_price")
            or item.get("strike")
            or item.get("strikePrice")
            or item.get("exercise_price")
        )
        if strike_price is None:
            continue

        strike_distance = abs(strike_price - target) if target is not None else 0.0
        if request_expiration and item_expiration == request_expiration:
            exact_candidates.append((strike_distance, 0.0, item))
            continue

        if requested_date is not None and item_expiration is not None:
            try:
                expiry_date = datetime.strptime(item_expiration, "%Y-%m-%d")
            except ValueError:
                expiry_date = None
            if expiry_date is not None:
                expiry_delta = abs((expiry_date - requested_date).days)
                fallback_candidates.append((expiry_delta, strike_distance, float(expiry_delta), item))

        elif target is not None:
            fallback_candidates.append((strike_distance, 0.0, 0.0, item))

    if exact_candidates:
        exact_candidates.sort(key=lambda row: row[:2])
        return exact_candidates[0][2]

    if fallback_candidates:
        fallback_candidates.sort(key=lambda row: row[:3])
        return fallback_candidates[0][3]

    return None


def fetch_webull_option_chain(
    symbol: str,
    *,
    option_type: str = "CALL",
    expiration: str | None = None,
    target_strike: float | None = None,
    settings: Settings | None = None,
) -> dict[str, Any] | None:
    app_key = (settings.webull_app_key if settings else None) or os.getenv("WEBULL_APP_KEY")
    app_secret = (settings.webull_app_secret if settings else None) or os.getenv("WEBULL_APP_SECRET")
    if not app_key or not app_secret:
        return None

    endpoint = resolve_webull_endpoint(
        getattr(settings, "webull_trading_mode", None),
        getattr(settings, "webull_endpoint", None),
    )
    api_client = ApiClient(app_key, app_secret, "us")
    api_client.add_endpoint("us", endpoint)
    api_client.set_stream_logger(stream=sys.stdout)

    try:
        response = DataClient(api_client).instrument.get_option_contracts(
            category="US_OPTION",
            underlying_symbols=str(symbol or "").strip().upper(),
            option_type=option_type.upper(),
            start_date=expiration,
            end_date=expiration,
            page_size=100,
        )
        if response is not None and hasattr(response, "status_code") and response.status_code != 200:
            logger.warning("Webull option-chain lookup failed: %s %s", response.status_code, getattr(response, "text", ""))
            response = None

        payload = response.json() if response is not None and hasattr(response, "json") else response
        selected = select_valid_webull_option_contract(
            payload,
            symbol=symbol,
            option_type=option_type,
            expiration=expiration,
            target_strike=target_strike,
        )
        if selected is not None and isinstance(selected, dict):
            return selected

        if expiration is not None:
            fallback_response = DataClient(api_client).instrument.get_option_contracts(
                category="US_OPTION",
                underlying_symbols=str(symbol or "").strip().upper(),
                option_type=option_type.upper(),
                page_size=100,
            )
            if fallback_response is not None and hasattr(fallback_response, "status_code") and fallback_response.status_code != 200:
                logger.warning("Webull fallback option-chain lookup failed: %s %s", fallback_response.status_code, getattr(fallback_response, "text", ""))
                return None
            fallback_payload = fallback_response.json() if fallback_response is not None and hasattr(fallback_response, "json") else fallback_response
            fallback_selected = select_valid_webull_option_contract(
                fallback_payload,
                symbol=symbol,
                option_type=option_type,
                expiration=expiration,
                target_strike=target_strike,
            )
            if fallback_selected is not None and isinstance(fallback_selected, dict):
                return fallback_selected
        return None
    except Exception:
        logger.exception("Unable to query Webull option chain for %s", symbol)
        return None



def resolve_option_contract_symbol(
    symbol: str,
    *,
    direction: Literal["bullish", "bearish", "neutral"] = "bullish",
    price_reference: float | None = None,
    expiration_days: int = 30,
    expiration_date: str | None = None,
) -> str:
    ticker = str(symbol or "").strip().upper()
    if not ticker:
        raise ValueError("Option contract symbol cannot be empty")

    reference = float(price_reference) if price_reference is not None else 100.0
    option_type = "C" if direction in {"bullish", "neutral"} else "P"
    strike = round(reference / 5.0) * 5.0
    strike_code = f"{int(round(strike * 1000)):08d}"
    expiration = (datetime.now(UTC) + timedelta(days=max(expiration_days, 1))).strftime("%y%m%d")
    return f"{ticker}{expiration}{option_type}{strike_code}"


def build_option_trade_request(
    decision: TradingDecision,
    *,
    direction: Literal["bullish", "bearish", "neutral"] = "bullish",
    notional_usd: float | None = None,
    client_order_id: str | None = None,
    payload: dict[str, Any] | None = None,
):
    # local enums OrderClass, OrderSide, OrderType, TimeInForce defined in this module
    payload = payload or {}
    contract_symbol = resolve_option_contract_symbol(
        decision.symbol,
        direction=direction,
        price_reference=float(payload.get("levels", {}).get("entry") or payload.get("entry_price") or max(decision.notional_usd, 1.0) / 10.0),
    )

    requested_notional = round(float(notional_usd if notional_usd is not None else decision.notional_usd), 2)
    return SimpleNamespace(
        symbol=contract_symbol,
        notional=requested_notional,
        side=OrderSide.BUY if decision.action == "buy" else OrderSide.SELL,
        type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
        client_order_id=client_order_id or f"option-{decision.symbol.lower()}-{int(time.time())}",
        order_class=OrderClass.SIMPLE,
    )


class Ledger:
    """Small SQLite ledger for received events, decisions, and orders."""

    def __init__(self, database_path: str) -> None:
        self.path = Path(database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def initialize(self) -> None:
        with closing(sqlite3.connect(self.path, timeout=10)) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    fingerprint TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    decision_json TEXT,
                    order_json TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS optionomics_trade_ideas (
                    trade_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    strategy TEXT,
                    pipeline_name TEXT,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    decision_json TEXT,
                    order_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def save_trade_idea(self, trade_id: str, payload: dict[str, Any], *, status: str = "queued") -> None:
        now = utc_now_iso()
        payload_json = json.dumps(payload, sort_keys=True)

        with closing(sqlite3.connect(self.path, timeout=10)) as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO optionomics_trade_ideas
                    (trade_id, symbol, direction, strategy, pipeline_name, payload_json, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade_id,
                    str(payload.get("symbol") or ""),
                    str(payload.get("direction") or "neutral"),
                    str(payload.get("strategy") or ""),
                    str(payload.get("pipeline_name") or payload.get("pipeline_short_name") or ""),
                    payload_json,
                    status,
                    now,
                    now,
                ),
            )
            conn.commit()

    def is_trade_idea_seen(self, trade_id: str) -> bool:
        with closing(sqlite3.connect(self.path, timeout=10)) as conn:
            row = conn.execute(
                "SELECT 1 FROM optionomics_trade_ideas WHERE trade_id = ? and status = 'ordered'",
                (trade_id,),
            ).fetchone()
        return row is not None

    def has_trade_or_symbol_seen(self, *, trade_id: str | None = None, symbol: str | None = None) -> bool:
        if trade_id is None and symbol is None:
            return False

        if trade_id is None:
            return False

        with closing(sqlite3.connect(self.path, timeout=10)) as conn:
            row = conn.execute(
                "SELECT 1 FROM optionomics_trade_ideas WHERE trade_id = ? LIMIT 1",
                (str(trade_id),),
            ).fetchone()
        return row is not None

    def mark_trade_idea_status(self, trade_id: str, *, status: str, decision: TradingDecision | None = None, order_payload: dict[str, Any] | None = None) -> None:
        now = utc_now_iso()
        decision_json = json.dumps(decision.model_dump(mode="json"), sort_keys=True) if decision else None
        order_json = json.dumps(order_payload or {}, sort_keys=True)

        with closing(sqlite3.connect(self.path, timeout=10)) as conn:
            conn.execute(
                """
                UPDATE optionomics_trade_ideas
                   SET status = ?, decision_json = ?, order_json = ?, updated_at = ?
                 WHERE trade_id = ?
                """,
                (status, decision_json, order_json, now, trade_id),
            )
            conn.commit()

    def reserve(self, fingerprint: str, payload: TradeIdea) -> bool:
        now = utc_now_iso()
        payload_json = json.dumps(payload.model_dump(mode="json"), sort_keys=True)

        with closing(sqlite3.connect(self.path, timeout=10)) as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO events
                    (fingerprint, status, payload_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (fingerprint, "queued", payload_json, now, now),
            )
            conn.commit()

        return cursor.rowcount == 1

    def finish(
        self,
        fingerprint: str,
        status: str,
        decision: TradingDecision,
        order_payload: dict[str, Any] | None = None,
    ) -> None:
        now = utc_now_iso()
        decision_json = json.dumps(decision.model_dump(mode="json"), sort_keys=True)
        order_json = json.dumps(order_payload or {}, sort_keys=True)

        with closing(sqlite3.connect(self.path, timeout=10)) as conn:
            conn.execute(
                """
                                UPDATE events
                                     SET status = ?, decision_json = ?, order_json = ?, error = NULL, updated_at = ?
                                 WHERE fingerprint = ?
                """,
                (status, decision_json, order_json, now, fingerprint),
            )
            conn.commit()

    def fail(self, fingerprint: str, error: str) -> None:
        now = utc_now_iso()

        with closing(sqlite3.connect(self.path, timeout=10)) as conn:
            conn.execute(
                """
                                UPDATE events
                                     SET status = ?, error = ?, updated_at = ?
                                 WHERE fingerprint = ?
                """,
                ("failed", error[:1000], now, fingerprint),
            )
            conn.commit()


app = FastAPI(title="Optionomics Trade Ideas Trading Bot", version="1.0.0")


def poll_optionomics_trade_ideas() -> list[dict[str, Any]]:
    settings = get_settings()
    email = os.getenv("OPTIONOMICS_EMAIL") or "you@example.com"
    try:
        ideas = fetch_trade_ideas(email, api_url=settings.optionomics_api_url, timeout=30)
    except RuntimeError as exc:
        logger.exception("Unable to fetch Optionomics trade ideas: %s", exc)
        return []

    ledger = Ledger(settings.database_path)

    for idea in ideas:
        try:
            #logger.info("Optionomics raw idea payload: %s", idea)
            trade_id = idea.get("id")
            symbol = idea.get("symbol")
            logger.info("Processing Optionomics idea: trade_id=%s symbol=%s", trade_id, color_symbol(symbol))
            if trade_id is None and symbol is None:
                #logger.warning("Skipping invalid Optionomics payload with no id or symbol: %s", idea)
                continue

            trade_id = str(trade_id if trade_id is not None else symbol or "unknown")
            if settings.force_reprocess:
                logger.warning("FORCE_REPROCESS=true; bypassing dedupe for Optionomics trade: trade_id=%s symbol=%s", trade_id, color_symbol(symbol))
            elif ledger.is_trade_idea_seen(trade_id):
                logger.info("Skipping already-processed Optionomics trade: trade_id=%s symbol=%s", trade_id, color_symbol(symbol))
                continue

            ledger.save_trade_idea(trade_id, idea, status="queued")
            decision = build_trade_decision_from_optionomics_payload(idea, settings)
            if decision.action == "skip":
                ledger.mark_trade_idea_status(trade_id, status="skipped", decision=decision)
                logger.info("Skipping %s from Optionomics: %s", decision.symbol, decision.rationale)
                continue

            if not validate_optionomics_symbol_match(idea, decision):
                ledger.mark_trade_idea_status(trade_id, status="skipped", decision=decision)
                logger.warning(
                    "Skipping Optionomics trade %s because decision symbol %s does not match payload symbol %s",
                    trade_id,
                    color_error(decision.symbol),
                    color_error(str(idea.get("symbol") or "").upper()),
                )
                continue

            payload = TradeIdea(
                alert_name=str(idea.get("id") or idea.get("symbol") or "optionomics"),
                source="trade_idea",
                symbol=decision.symbol,
                direction=idea.get("direction", "neutral"),
                strategy=idea.get("strategy") or "optionomics",
                entry_price=idea.get("levels", {}).get("entry"),
                target_price=idea.get("levels", {}).get("target"),
                stop_price=idea.get("levels", {}).get("stop"),
                triggered_at=datetime.now(UTC),
                matched_criteria={"pipeline_short_name": idea.get("pipeline_short_name"), "pipeline_name": idea.get("pipeline_name")},
            )
            order_payload = maybe_submit_order(
                ledger,
                decision,
                settings,
                payload,
                trade_id=trade_id,
            )
            if order_payload is None:
                continue

            logger.info("Optionomics %s decision: %s -> %s", decision.symbol, decision.strategy, order_payload)
        except Exception as exc:
            ledger.mark_trade_idea_status(trade_id, status="failed", decision=build_trade_decision_from_optionomics_payload(idea, settings) if "direction" in idea else None)
            logger.exception("Failed to process Optionomics idea: %s", color_error(str(idea)))
            logger.warning("Marked Optionomics trade %s as failed because broker submission raised: %s", trade_id, exc)

    return ideas


@app.on_event("startup")
def start_optionomics_polling() -> None:
    settings = get_settings()
    should_poll = bool(os.getenv("OPTIONOMICS_API_KEY") and os.getenv("OPTIONOMICS_EMAIL")) and settings.optionomics_poll_enabled
    if not should_poll:
        reason = "disabled by OPTIONOMICS_POLL_ENABLED" if not settings.optionomics_poll_enabled else "missing OPTIONOMICS_API_KEY or OPTIONOMICS_EMAIL"
        logger.info("Optionomics polling disabled: %s", reason)
        return

    interval_seconds = settings.optionomics_poll_interval_seconds
    logger.info("Starting Optionomics polling every %s seconds", interval_seconds)

    def runner() -> None:
        while True:
            try:
                if is_market_open_et():
                    poll_optionomics_trade_ideas()
                else:
                    logger.info("Skipping Optionomics poll; market closed (ET)")
            except Exception:
                logger.exception("Polling loop crashed")
            time.sleep(interval_seconds)

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()


@lru_cache
def get_settings() -> Settings:
    return Settings()


def maybe_submit_order(
    ledger: Ledger,
    decision: TradingDecision,
    settings: Settings,
    payload: TradeIdea,
    *,
    trade_id: str | None = None,
) -> dict[str, Any] | None:
    if payload.direction == "neutral":
        from app.main import maybe_submit_order as submit_shared_neutral
        from app.persistence.ledger import Ledger as SharedLedger
        return submit_shared_neutral(SharedLedger(settings.database_path), decision, settings, payload, trade_id=trade_id)

    # The polling loop performs the duplicate check before the trade is inserted
    # into the ledger. This helper should submit the current trade without
    # falsely treating the just-inserted row as a duplicate.
    order_payload = submit_paper_order(decision, settings, fingerprint_for(payload), payload)
    if trade_id is not None:
        status = "dry_run" if settings.dry_run else "ordered"
        ledger.mark_trade_idea_status(trade_id, status=status, decision=decision, order_payload=order_payload)
    return order_payload


def fingerprint_for(payload: TradeIdea) -> str:
    material = "|".join(
        [
            payload.alert_name,
            payload.source,
            payload.symbol,
            payload.direction,
            payload.strategy,
            payload.triggered_at.isoformat(),
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def normalize_confidence(value: Any, *, default: float = 1.0) -> float:
    try:
        numeric = float(value) if value is not None else default
    except (TypeError, ValueError):
        return default

    if numeric != numeric or numeric in (float("inf"), float("-inf")):
        return default

    return round(max(0.0, min(1.0, numeric)), 4)


def validate_optionomics_symbol_match(payload: dict[str, Any], decision: TradingDecision) -> bool:
    payload_symbol = str(payload.get("symbol") or "").strip().upper()
    decision_symbol = str(decision.symbol or "").strip().upper()
    if not payload_symbol or not decision_symbol:
        return False
    return payload_symbol == decision_symbol


def build_trade_decision(payload: TradeIdea, settings: Settings) -> TradingDecision:
    action = "skip"
    rationale = "No trade action determined."
    risk_notes: list[str] = []

    if payload.direction == "bullish":
        action = "buy"
        rationale = "Execute bullish trade idea as a paper equity buy."
    elif payload.direction == "bearish":
        if settings.allow_short_selling:
            action = "sell_short"
            rationale = "Execute bearish trade idea as a paper equity short sale."
        else:
            rationale = "Short selling disabled; skipping bearish trade idea."
            risk_notes.append("Short selling disabled")
    else:
        rationale = "Neutral trade idea; skipping execution."
        risk_notes.append("Neutral trade idea")

    if action != "skip":
        if payload.entry_price is None or payload.target_price is None or payload.stop_price is None:
            action = "skip"
            rationale = "Missing entry, target, or stop levels."
            risk_notes.append("Missing price levels")
        elif action == "buy":
            if payload.target_price <= payload.entry_price or payload.stop_price >= payload.entry_price:
                action = "skip"
                rationale = "Bullish trade idea has invalid price levels."
                risk_notes.append("Bullish price levels invalid")
        elif action == "sell_short":
            if payload.target_price >= payload.entry_price or payload.stop_price <= payload.entry_price:
                action = "skip"
                rationale = "Bearish trade idea has invalid price levels."
                risk_notes.append("Bearish price levels invalid")

    notional = settings.max_notional_usd if action != "skip" else 0.0
    return TradingDecision(
        action=action,
        symbol=payload.symbol,
        notional_usd=round(notional, 2),
        confidence=1.0,
        rationale=rationale,
        risk_notes=risk_notes,
    )


def validate_optionomics_directional_levels(direction: Literal["bullish", "bearish", "neutral"], entry: float, target: float, stop: float) -> bool:
    if direction == "bullish":
        return target > entry and stop < entry
    if direction == "bearish":
        return target < entry and stop > entry
    if direction == "neutral":
        return target < entry and stop > entry
    return False


def build_trade_decision_from_optionomics_payload(payload: dict[str, Any], settings: Settings) -> TradingDecision:
    idea = OptionomicsTradeIdea.model_validate(payload)
    levels = idea.levels
    pipeline = (idea.pipeline_short_name or idea.pipeline_name or "placeholder").strip()
    strategy_name = "iron_condor" if (idea.direction == "neutral" or pipeline.lower() == "crush") else "placeholder"

    normalized_confidence = normalize_confidence(idea.confidence_score, default=1.0)

    if idea.direction == "bullish":
        action: Literal["skip", "buy", "sell_short"] = "buy"
        rationale = f"Optionomics {pipeline} idea: bullish setup flagged for {strategy_name} execution."
    elif idea.direction == "bearish":
        if settings.allow_short_selling:
            action = "sell_short"
            rationale = f"Optionomics {pipeline} idea: bearish setup flagged for {strategy_name} execution."
        else:
            action = "skip"
            rationale = "Short selling disabled; skipping bearish Optionomics trade idea."
            return TradingDecision(
                action=action,
                symbol=idea.symbol,
                strategy=strategy_name,
                notional_usd=0.0,
                confidence=normalized_confidence,
                rationale=rationale,
                risk_notes=["Short selling disabled"],
            )
    else:
        action = "buy"
        rationale = f"Optionomics {pipeline} idea: neutral setup selected for iron_condor strategy execution."

    if action == "buy" and (idea.direction == "neutral" or pipeline.lower() == "crush"):
        logger.info("Optionomics signal selected strategy=%s for symbol=%s", strategy_name, idea.symbol)

    entry = levels.get("entry")
    target = levels.get("target")
    stop = levels.get("stop")
    current = levels.get("current")
    peak = levels.get("peak")

    if entry is None or target is None or stop is None:
        return TradingDecision(
            action="skip",
            symbol=idea.symbol,
            strategy=strategy_name,
            notional_usd=0.0,
            confidence=normalized_confidence,
            rationale="Optionomics levels missing required entry, target, and stop values.",
            risk_notes=["Missing price levels"],
        )

    if action == "buy":
        if not validate_optionomics_directional_levels(idea.direction, entry, target, stop):
            message = (
                "Neutral Optionomics iron-condor idea has invalid price levels."
                if idea.direction == "neutral"
                else "Bullish Optionomics idea has invalid price levels."
            )
            return TradingDecision(
                action="skip",
                symbol=idea.symbol,
                strategy=strategy_name,
                notional_usd=0.0,
                confidence=normalized_confidence,
                rationale=message,
                risk_notes=["Neutral iron-condor price levels invalid" if idea.direction == "neutral" else "Bullish price levels invalid"],
            )
    else:
        if not validate_optionomics_directional_levels(idea.direction, entry, target, stop):
            return TradingDecision(
                action="skip",
                symbol=idea.symbol,
                strategy=strategy_name,
                notional_usd=0.0,
                confidence=normalized_confidence,
                rationale="Bearish Optionomics idea has invalid price levels.",
                risk_notes=["Bearish price levels invalid"],
            )

    notes = [f"Pipeline={pipeline}", f"Entry={entry}", f"Target={target}", f"Stop={stop}", f"Current={current}", f"Peak={peak}"]
    notional = settings.max_notional_usd
    return TradingDecision(
        action=action,
        symbol=idea.symbol,
        strategy=strategy_name,
        notional_usd=round(notional, 2),
        confidence=normalized_confidence,
        rationale=f"{rationale} Strategy selected: {strategy_name}. Levels: {', '.join(notes)}",
        risk_notes=notes,
    )


def skip_decision(decision: TradingDecision, reason: str) -> TradingDecision:
    notes = [*decision.risk_notes, reason]
    rationale = f"{decision.rationale} Risk gate: {reason}"
    return decision.model_copy(
        update={
            "action": "skip",
            "notional_usd": 0.0,
            "rationale": rationale,
            "risk_notes": notes,
        }
    )


def apply_risk_gates(
    payload: TradeIdea,
    decision: TradingDecision,
    settings: Settings,
) -> TradingDecision:
    if decision.symbol != payload.symbol:
        return skip_decision(decision, "Decision symbol does not match payload symbol")

    if decision.action == "skip":
        return decision.model_copy(update={"notional_usd": 0.0})

    if payload.entry_price is None or payload.target_price is None or payload.stop_price is None:
        return skip_decision(decision, "Entry, target, and stop levels are required")

    if decision.action == "buy":
        if payload.direction != "bullish":
            return skip_decision(decision, "Buy action is allowed only for bullish trade ideas")
        if payload.target_price <= payload.entry_price:
            return skip_decision(decision, "Bullish target must be above entry")
        if payload.stop_price >= payload.entry_price:
            return skip_decision(decision, "Bullish stop must be below entry")

    if decision.action == "sell_short":
        if not settings.allow_short_selling:
            return skip_decision(decision, "Short selling is disabled")
        if payload.direction != "bearish":
            return skip_decision(decision, "Short action is allowed only for bearish trade ideas")
        if payload.target_price >= payload.entry_price:
            return skip_decision(decision, "Bearish target must be below entry")
        if payload.stop_price <= payload.entry_price:
            return skip_decision(decision, "Bearish stop must be above entry")

    if decision.notional_usd <= 0:
        return skip_decision(decision, "Order notional must be positive")

    notional = min(decision.notional_usd, settings.max_notional_usd)
    return decision.model_copy(update={"notional_usd": round(notional, 2)})


def _load_webull_combo_module() -> Any:
    from importlib import import_module
    return import_module('app.options.brackets')


def _load_webull_option_chain_module() -> Any:
    from importlib import import_module
    return import_module('app.options.chain')


def get_webull_valid_expiry(
    symbol: str,
    *,
    option_type: str = "CALL",
    requested_expiry: str | None = None,
    settings: Settings | None = None,
) -> str | None:
    if not requested_expiry:
        return None

    module = _load_webull_option_chain_module()
    app_key = (settings.webull_app_key if settings else None) or os.getenv("WEBULL_APP_KEY")
    app_secret = (settings.webull_app_secret if settings else None) or os.getenv("WEBULL_APP_SECRET")
    if not app_key or not app_secret:
        return None

    endpoint = resolve_webull_endpoint(
        getattr(settings, "webull_trading_mode", None),
        getattr(settings, "webull_endpoint", None),
    )
    api_client = ApiClient(app_key, app_secret, "us")
    api_client.add_endpoint("us", endpoint)
    api_client.set_stream_logger(stream=sys.stdout)
    data_client = module.DataClient(api_client)

    try:
        selected_expiry, _ = module.get_valid_webull_option_chain(
            data_client,
            symbol=symbol,
            option_type=option_type,
            requested_expiry=requested_expiry,
        )
        return selected_expiry
    except Exception:
        logger.exception("Unable to resolve Webull expiry from option chain for %s", symbol)
        return None


def _is_webull_rate_limit_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return "429" in message or "too_many_requests" in message or "too many requests" in message or "rate limit" in message


def submit_paper_order(
    decision: TradingDecision,
    settings: Settings,
    fingerprint: str,
    payload: TradeIdea | None = None,
) -> dict[str, Any]:
    if payload is not None and payload.direction in {"neutral", "bearish"}:
        from app.execution.submitter import submit_paper_order as submit_shared_order
        return submit_shared_order(decision, settings, fingerprint, payload)

    client_order_id = f"om-{fingerprint[:24]}"
    logger.info("Processing paper order for %s", decision.symbol)
    if settings.dry_run:
        logger.info("DRY_RUN=true; broker order suppressed for %s", decision.symbol)
        return {
            "dry_run": True,
            "client_order_id": client_order_id,
            "symbol": decision.symbol,
            "action": decision.action,
            "notional_usd": decision.notional_usd,
        }

    webull_module = _load_webull_combo_module()
    account_id = (options_margin_account_id(webull_module, settings)
                  if decision.action == "sell_short" or settings.webull_trading_mode == "live" else webull_module.get_account_id())

    reference_level = None
    if payload is not None:
        reference_level = payload.entry_price or payload.target_price or payload.stop_price
    if reference_level is None:
        reference_level = max(float(decision.notional_usd) / 100.0, 1.0)

    expiry = (datetime.now(UTC) + timedelta(days=5)).strftime("%Y-%m-%d")
    strike = round(float(reference_level) / 5.0) * 5.0
    option_type = "CALL" if decision.action == "buy" else "PUT"
    resolved_expiry = get_webull_valid_expiry(
        decision.symbol,
        option_type=option_type,
        requested_expiry=expiry,
        settings=settings,
    ) or expiry
    validated_contract = fetch_webull_option_chain(
        decision.symbol,
        option_type=option_type,
        expiration=resolved_expiry,
        target_strike=strike,
        settings=settings,
    )
    if not isinstance(validated_contract, dict):
        raise ValueError(f"Unable to find a valid Webull {decision.symbol} {option_type} contract for {expiry}.")

    selected_strike = _coerce_float(
        validated_contract.get("strike_price")
        or validated_contract.get("strike")
        or validated_contract.get("strikePrice")
    )
    selected_expiration = _normalize_expiration(
        validated_contract.get("expiration_date")
        or validated_contract.get("expiration")
        or validated_contract.get("exp_date")
        or validated_contract.get("expire_date")
        or validated_contract.get("expiry")
        or expiry,
    )
    if selected_strike is None:
        selected_strike = strike
    entry_limit = max(float(selected_strike) * 0.08, 1.0)
    quantity = 1

    profit_percent, stop_loss_percent = exit_percentages(
        settings, "bullish" if decision.action == "buy" else "bearish"
    )
    if decision.action == "buy":
        order_result = webull_module.buy_call_with_bracket(
            account_id=account_id,
            symbol=decision.symbol,
            strike=selected_strike,
            expiration=selected_expiration,
            quantity=quantity,
            entry_limit=entry_limit,
            profit_percent=profit_percent,
            stop_loss_percent=stop_loss_percent,
        )
    else:
        from app.bearish.executor import BearishPutOptionExecutor

        executor = BearishPutOptionExecutor(module=webull_module)
        order_result = executor.submit(
            account_id=account_id,
            symbol=decision.symbol,
            strike=selected_strike,
            expiration=selected_expiration,
            quantity=quantity,
            profit_percent=profit_percent,
            stop_loss_percent=stop_loss_percent,
        )

    logger.info(
        "Webull combo order submitted successfully: symbol=%s side=%s notional_usd=%s order_id=%s",
        decision.symbol,
        "BUY" if decision.action == "buy" else "SELL",
        decision.notional_usd,
        str(order_result.get("order_id") or order_result.get("client_order_id") or ""),
    )

    return {
        "dry_run": False,
        "id": str(order_result.get("order_id") or order_result.get("client_order_id") or ""),
        "client_order_id": client_order_id,
        "symbol": decision.symbol,
        "status": "submitted",
        "side": "BUY" if decision.action == "buy" else "SELL",
        "notional_usd": decision.notional_usd,
        "broker": "webull",
    }


def process_trade_idea(payload: TradeIdea, fingerprint: str) -> None:
    settings = get_settings()
    ledger = Ledger(settings.database_path)

    try:
        decision = build_trade_decision(payload, settings)
        gated_decision = apply_risk_gates(payload, decision, settings)

        if gated_decision.action == "skip":
            ledger.finish(fingerprint, "skipped", gated_decision)
            logger.info("Skipped %s: %s", payload.symbol, gated_decision.rationale)
            return

        order_payload = maybe_submit_order(ledger, gated_decision, settings, payload)
        if order_payload is None:
            ledger.finish(fingerprint, "skipped", gated_decision)
            logger.info("Skipped duplicate order for %s", payload.symbol)
            return

        final_status = "dry_run" if settings.dry_run else "ordered"
        ledger.finish(fingerprint, final_status, gated_decision, order_payload)
        logger.info("Processed %s with status %s", payload.symbol, final_status)
    except Exception as exc:
        ledger.fail(fingerprint, str(exc))
        logger.exception("Failed to process event %s", fingerprint)
        raise


@app.get("/health")
def health() -> dict[str, Any]:
    settings = get_settings()
    return {
        "status": "ok",
        "dry_run": settings.dry_run,
    }
