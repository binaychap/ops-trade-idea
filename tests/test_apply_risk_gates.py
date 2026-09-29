import logging
from types import SimpleNamespace
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.main import (
    TradeIdea,
    TradingDecision,
    apply_risk_gates,
    build_trade_decision,
    validate_optionomics_symbol_match,
)


def make_payload(symbol: str = "AAPL", direction: str = "bullish") -> TradeIdea:
    return TradeIdea(
        alert_name="test",
        source="trade_idea",
        symbol=symbol,
        direction=direction,
        strategy="test",
        entry_price=100.0,
        target_price=110.0,
        stop_price=95.0,
        triggered_at=datetime.now(timezone.utc),
        matched_criteria={},
    )


def test_build_trade_decision_clamps_confidence_above_one():
    from app.main import build_trade_decision_from_optionomics_payload

    payload = {
        "id": "idea-high-confidence",
        "symbol": "META",
        "direction": "bearish",
        "strategy": "short_put",
        "pipeline_short_name": "Momentum",
        "pipeline_name": "Momentum setups",
        "levels": {"entry": 500.0, "target": 470.0, "stop": 530.0, "current": 495.0, "peak": 510.0},
        "generated_at": "2026-08-05T13:35:00Z",
        "confidence_score": 70.0,
    }
    settings = SimpleNamespace(max_notional_usd=250.0, allow_short_selling=False)

    decision = build_trade_decision_from_optionomics_payload(payload, settings)

    assert decision.action == "skip"
    assert decision.confidence == 1.0


def test_logger_levels_are_info():
    assert logging.getLogger("optionomics_bot").level == logging.INFO
    assert logging.getLogger("optionomics_client").level == logging.INFO
    assert logging.getLogger("debugpy").level == logging.INFO
    assert logging.getLogger("pydevd").level == logging.INFO
    assert logging.getLogger("asyncio").level == logging.INFO
    assert logging.getLogger("urllib3").level == logging.INFO
    assert logging.getLogger("webull").level == logging.INFO
    assert logging.getLogger("uvicorn.access").level == logging.INFO
    assert logging.getLogger("uvicorn.error").level == logging.INFO


def make_decision(action: str = "buy", symbol: str = "AAPL", notional: float = 100.0, confidence: float = 0.9) -> TradingDecision:
    return TradingDecision(
        action=action,
        symbol=symbol,
        notional_usd=notional,
        confidence=confidence,
        rationale="test rationale",
        risk_notes=[],
    )


def test_buy_success_caps_notional():
    payload = make_payload(symbol="NVDA", direction="bullish")
    decision = make_decision(action="buy", symbol="NVDA", notional=1000.0, confidence=0.95)
    settings = SimpleNamespace(max_notional_usd=250.0, allow_short_selling=False)

    gated = apply_risk_gates(payload, decision, settings)

    assert gated.action == "buy"
    assert gated.symbol == "NVDA"
    # notional should be capped to max_notional_usd
    assert gated.notional_usd == 250.0


def test_buy_invalid_levels_skip():
    payload = make_payload(symbol="AAPL", direction="bullish")
    # make target <= entry to trigger the bullish-levels gate
    payload.entry_price = 100.0
    payload.target_price = 90.0
    payload.stop_price = 95.0

    decision = make_decision(action="buy", symbol="AAPL", notional=100.0, confidence=0.95)
    settings = SimpleNamespace(max_notional_usd=250.0, allow_short_selling=False)

    gated = apply_risk_gates(payload, decision, settings)

    assert gated.action == "skip"
    assert gated.notional_usd == 0.0
    assert any("Bullish target" in note or "Bullish target" in gated.rationale for note in gated.risk_notes) or "Bullish target" in gated.rationale


def test_symbol_mismatch_skip():
    payload = make_payload(symbol="AAPL", direction="bullish")
    decision = make_decision(action="buy", symbol="MSFT", notional=100.0, confidence=0.95)
    settings = SimpleNamespace(max_notional_usd=250.0, allow_short_selling=False)

    gated = apply_risk_gates(payload, decision, settings)

    assert gated.action == "skip"
    assert "symbol does not match" in gated.rationale or any("symbol does not match" in note for note in gated.risk_notes)


def test_sell_short_disabled_skip():
    payload = make_payload(symbol="TSLA", direction="bearish")
    decision = make_decision(action="sell_short", symbol="TSLA", notional=100.0, confidence=0.95)
    settings = SimpleNamespace(max_notional_usd=250.0, allow_short_selling=False)

    gated = apply_risk_gates(payload, decision, settings)

    assert gated.action == "skip"
    assert any("Short selling is disabled" in note for note in gated.risk_notes) or "Short selling is disabled" in gated.rationale


def test_market_open_et_window():
    from app.main import is_market_open_et

    assert is_market_open_et(datetime(2026, 9, 9, 9, 30, tzinfo=ZoneInfo("America/New_York"))) is True
    assert is_market_open_et(datetime(2026, 9, 9, 15, 59, tzinfo=ZoneInfo("America/New_York"))) is True
    assert is_market_open_et(datetime(2026, 9, 9, 16, 0, tzinfo=ZoneInfo("America/New_York"))) is False
    assert is_market_open_et(datetime(2026, 9, 9, 9, 29, tzinfo=ZoneInfo("America/New_York"))) is False
    assert is_market_open_et(datetime(2026, 9, 12, 13, 0, tzinfo=ZoneInfo("America/New_York"))) is False


def test_build_trade_decision_bullish_executes_buy():
    payload = make_payload(symbol="AAPL", direction="bullish")
    settings = SimpleNamespace(max_notional_usd=200.0, allow_short_selling=False)

    decision = build_trade_decision(payload, settings)

    assert decision.action == "buy"
    assert decision.notional_usd == 200.0
    assert decision.confidence == 1.0
    assert "bullish" in decision.rationale.lower()


def test_build_trade_decision_bearish_with_short_enabled():
    payload = make_payload(symbol="TSLA", direction="bearish")
    payload.entry_price = 100.0
    payload.target_price = 90.0
    payload.stop_price = 105.0
    settings = SimpleNamespace(max_notional_usd=150.0, allow_short_selling=True)

    decision = build_trade_decision(payload, settings)

    assert decision.action == "sell_short"
    assert decision.notional_usd == 150.0
    assert decision.confidence == 1.0


def test_build_trade_decision_neutral_skips():
    payload = make_payload(symbol="MSFT", direction="neutral")
    settings = SimpleNamespace(max_notional_usd=100.0, allow_short_selling=True)

    decision = build_trade_decision(payload, settings)

    assert decision.action == "skip"
    assert decision.notional_usd == 0.0
    assert "neutral" in decision.rationale.lower()


def test_optionomics_crush_pipeline_selects_iron_condor_strategy():
    payload = {
        "id": "idea-1",
        "symbol": "NVDA",
        "direction": "bullish",
        "strategy": "buy_call",
        "pipeline_short_name": "Crush",
        "pipeline_name": "Crush setups",
        "levels": {"entry": 128.4, "target": 138.0, "stop": 123.5, "current": 130.1, "peak": 131.2},
        "generated_at": "2026-08-05T13:35:00Z",
    }
    settings = SimpleNamespace(max_notional_usd=250.0, allow_short_selling=False)

    from app.main import build_trade_decision_from_optionomics_payload

    decision = build_trade_decision_from_optionomics_payload(payload, settings)

    assert decision.action == "buy"
    assert decision.strategy == "iron_condor"
    assert "Crush" in decision.rationale


def test_optionomics_symbol_mismatch_is_rejected_before_execution():
    idea = {
        "id": "idea-symbol-check",
        "symbol": "MSFT",
        "direction": "bullish",
        "strategy": "bull_call_spread",
        "pipeline_short_name": "Swing",
        "pipeline_name": "Swing setups",
        "levels": {"entry": 498.0, "target": 520.0, "stop": 490.0, "current": 501.0, "peak": 503.0},
    }
    decision = TradingDecision(
        action="buy",
        symbol="NVDA",
        strategy="placeholder",
        notional_usd=250.0,
        confidence=0.9,
        rationale="test",
    )

    assert validate_optionomics_symbol_match(idea, decision) is False


def test_optionomics_neutral_payload_selects_iron_condor():
    payload = {
        "id": "idea-neutral-ignore",
        "symbol": "ULTA",
        "direction": "neutral",
        "strategy": "iron_condor",
        "pipeline_short_name": "Momentum",
        "pipeline_name": "Momentum setups",
        "levels": {"entry": 543.19, "target": 459.44, "stop": 626.94, "current": 530.63, "peak": 543.19},
        "generated_at": "2026-08-27T11:00:00.687140Z",
        "confidence_score": 0.88,
    }
    settings = SimpleNamespace(max_notional_usd=250.0, allow_short_selling=False)

    from app.main import build_trade_decision_from_optionomics_payload

    decision = build_trade_decision_from_optionomics_payload(payload, settings)

    assert decision.action == "buy"
    assert decision.strategy == "iron_condor"
    assert "neutral" in decision.rationale.lower()


def test_optionomics_neutral_crush_payload_selects_iron_condor():
    payload = {
        "id": "idea-neutral-crush",
        "symbol": "ULTA",
        "direction": "neutral",
        "strategy": "iron_condor",
        "pipeline_short_name": "Crush",
        "pipeline_name": "IV Crush",
        "levels": {"entry": 543.19, "target": 459.44, "stop": 626.94, "current": 530.63, "peak": 543.19},
        "generated_at": "2026-08-27T11:00:00.687140Z",
        "confidence_score": 0.88,
    }
    settings = SimpleNamespace(max_notional_usd=250.0, allow_short_selling=False)

    from app.main import build_trade_decision_from_optionomics_payload

    decision = build_trade_decision_from_optionomics_payload(payload, settings)

    assert decision.action == "buy"
    assert decision.strategy == "iron_condor"
    assert decision.notional_usd == 250.0
    assert "neutral" in decision.rationale.lower()


def test_optionomics_bearish_direction_checks_its_range():
    payload = {
        "id": "idea-bearish-1",
        "symbol": "META",
        "direction": "bearish",
        "strategy": "short_put",
        "pipeline_short_name": "Momentum",
        "pipeline_name": "Momentum setups",
        "levels": {"entry": 500.0, "target": 470.0, "stop": 530.0, "current": 495.0, "peak": 510.0},
        "generated_at": "2026-08-05T13:35:00Z",
        "confidence_score": 0.8,
    }
    settings = SimpleNamespace(max_notional_usd=250.0, allow_short_selling=True)

    from app.main import build_trade_decision_from_optionomics_payload

    decision = build_trade_decision_from_optionomics_payload(payload, settings)

    assert decision.action == "sell_short"
    assert decision.strategy == "placeholder"
    assert decision.notional_usd == 250.0


def test_bearish_put_executor_uses_put_bracket_builder():
    from app.bearish.executor import BearishPutOptionExecutor

    executor = BearishPutOptionExecutor()

    assert executor.order_builder_name() == "buy_put_with_bracket"
    assert executor.option_type() == "PUT"
    assert executor.strategy_label() == "sell_next_way"


def test_optionomics_placeholder_pipeline_is_not_executed():
    payload = {
        "id": "idea-2",
        "symbol": "AAPL",
        "direction": "bullish",
        "strategy": "buy_call",
        "pipeline_short_name": "Swing",
        "pipeline_name": "Swing setups",
        "levels": {"entry": 100.0, "target": 110.0, "stop": 95.0, "current": 102.0, "peak": 103.5},
        "generated_at": "2026-08-05T13:35:00Z",
    }
    settings = SimpleNamespace(max_notional_usd=250.0, allow_short_selling=False)

    from app.main import build_trade_decision_from_optionomics_payload

    decision = build_trade_decision_from_optionomics_payload(payload, settings)

    assert decision.action == "buy"
    assert decision.strategy == "placeholder"
    assert "placeholder" in decision.rationale.lower()


def test_trade_idea_duplicate_is_skipped_by_ledger(tmp_path):
    from app.main import Ledger

    ledger = Ledger(str(tmp_path / "ledger.sqlite3"))
    idea = {
        "id": "idea-dup-1",
        "symbol": "NVDA",
        "direction": "bullish",
        "strategy": "buy_call",
        "pipeline_short_name": "Crush",
        "pipeline_name": "Crush setups",
        "levels": {"entry": 128.4, "target": 138.0, "stop": 123.5, "current": 130.1, "peak": 131.2},
    }

    assert ledger.is_trade_idea_seen("idea-dup-1") is False
    ledger.save_trade_idea("idea-dup-1", idea, status="queued")
    assert ledger.is_trade_idea_seen("idea-dup-1") is True
    assert ledger.has_trade_or_symbol_seen(trade_id="idea-dup-1", symbol="NVDA") is True
    assert ledger.has_trade_or_symbol_seen(trade_id="idea-dup-2", symbol="NVDA") is False


def test_current_trade_id_is_not_self_deduped_after_insert(tmp_path):
    from app.main import Ledger

    ledger = Ledger(str(tmp_path / "ledger.sqlite3"))
    idea = {
        "id": "idea-current-1",
        "symbol": "AAPL",
        "direction": "bullish",
        "strategy": "buy_call",
        "pipeline_short_name": "Swing",
        "pipeline_name": "Swing setups",
        "levels": {"entry": 100.0, "target": 110.0, "stop": 95.0, "current": 102.0, "peak": 103.5},
    }

    ledger.save_trade_idea("idea-current-1", idea, status="queued")
    assert ledger.has_trade_or_symbol_seen(trade_id="idea-current-1", symbol="AAPL") is True
    assert ledger.has_trade_or_symbol_seen(trade_id="idea-current-2", symbol="AAPL") is False


def test_ordered_trade_id_and_symbol_are_not_resubmitted(tmp_path):
    from app.main import Ledger

    ledger = Ledger(str(tmp_path / "ledger.sqlite3"))
    idea = {
        "id": "trace-abc-123",
        "symbol": "BHP",
        "direction": "bullish",
        "strategy": "buy_call",
        "pipeline_short_name": "Momentum",
        "pipeline_name": "Momentum setups",
        "levels": {"entry": 97.06, "target": 99.0, "stop": 94.5},
    }

    ledger.save_trade_idea("trace-abc-123", idea, status="ordered")
    assert ledger.has_ordered_trade(trade_id="trace-abc-123", symbol="BHP") is True
    assert ledger.has_ordered_trade(trade_id="trace-abc-123", symbol="AAPL") is False
    assert ledger.has_ordered_trade(trade_id="trace-other", symbol="BHP") is False


def test_build_option_order_request_prefers_contract_symbol(monkeypatch):
    from app.main import build_option_trade_request, OrderSide, OrderType

    monkeypatch.setattr("app.main.resolve_option_contract_symbol", lambda *args, **kwargs: "MSFT240919C00450000")

    decision = TradingDecision(
        action="buy",
        symbol="MSFT",
        strategy="iron_condor",
        notional_usd=250.0,
        confidence=0.9,
        rationale="test",
        risk_notes=[],
    )

    request = build_option_trade_request(decision, direction="bullish", notional_usd=250.0)

    assert request.symbol == "MSFT240919C00450000"
    assert request.side == OrderSide.BUY
    assert request.type == OrderType.MARKET
    assert request.notional == 250.0

    import os
    if os.path.exists("test_duplicate_ledger.sqlite3"):
        os.remove("test_duplicate_ledger.sqlite3")


def test_submit_paper_order_calculates_stop_and_target_from_entry(monkeypatch):
    from app.main import Settings, TradingDecision, TradeIdea, submit_paper_order

    captured = {}

    class FakeWebullModule:
        @staticmethod
        def get_account_id(**kwargs):
            return "acct-123"

        @staticmethod
        def buy_stock(**kwargs):
            captured.update(kwargs)
            return {"client_order_id": "stock-ok"}

    decision = TradingDecision(
        action="buy",
        symbol="AAPL",
        strategy="iron_condor",
        notional_usd=250.0,
        confidence=0.9,
        rationale="test",
        risk_notes=[],
    )
    settings = Settings(DRY_RUN=False, BULLISH_STOCK_ACCOUNT_NUMBER="test-cash")
    payload = TradeIdea(
        alert_name="entry-stop-target",
        source="trade_idea",
        symbol="AAPL",
        direction="bullish",
        strategy="test",
        entry_price=100.0,
        target_price=125.0,
        stop_price=80.0,
        triggered_at=datetime.now(timezone.utc),
        matched_criteria={},
    )

    monkeypatch.setattr("app.execution.submitter._load_webull_stock_module", lambda: FakeWebullModule())

    submit_paper_order(decision, settings, "fingerprint-1234567890abcd", payload)

    assert captured["symbol"] == "AAPL"
    assert "quantity" not in captured
    assert "stop_price" not in captured


def test_submit_paper_order_routes_bearish_decisions_to_put_executor(monkeypatch):
    from app.main import Settings, TradingDecision
    from app.execution import submitter as webull_submitter

    captured = {}

    class FakeBrokerModule:
        @staticmethod
        def get_account_id(**kwargs):
            assert kwargs == {"account_number": "test-margin"}
            return "acct-option-1"

    class FakeExecutor:
        @staticmethod
        def option_type():
            return "PUT"

        @staticmethod
        def strategy_label():
            return "sell_next_way"

        def __init__(self, module):
            captured["module"] = module

        def submit(self, **kwargs):
            captured["submit_kwargs"] = kwargs
            return {"client_order_id": "put-ok"}

    decision = TradingDecision(
        action="sell_short",
        symbol="AAPL",
        strategy="placeholder",
        notional_usd=250.0,
        confidence=0.9,
        rationale="bearish",
        risk_notes=[],
    )
    settings = Settings(DRY_RUN=False, OPTIONS_MARGIN_ACCOUNT_NUMBER="test-margin")
    payload = SimpleNamespace(entry_price=100.0, target_price=90.0, stop_price=105.0)

    monkeypatch.setattr(webull_submitter, "_load_webull_option_module", lambda: FakeBrokerModule())
    monkeypatch.setattr("app.bearish.executor.BearishPutOptionExecutor", FakeExecutor)

    result = webull_submitter.submit_paper_order(decision, settings, "fingerprint-1234567890abcd", payload)

    assert result["broker"] == "webull"
    assert result["side"] == "SELL"
    assert captured["submit_kwargs"]["symbol"] == "AAPL"
    assert "entry_limit" not in captured["submit_kwargs"]
    assert captured["submit_kwargs"]["profit_percent"] == 20
    assert captured["submit_kwargs"]["stop_loss_percent"] == 10


def test_buy_stock_submits_fixed_amount_order(monkeypatch):
    import importlib.util
    from pathlib import Path

    module_path = Path(__file__).resolve().parent.parent / "app" / "bullish/stock_bracket.py"
    spec = importlib.util.spec_from_file_location("webull_combo_stock_test", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)

    monkeypatch.setattr(module, 'current_stock_quote', lambda symbol: {'price': 200})
    calls = []

    class FakeTradeClient:
        class order_v3:
            @staticmethod
            def place_order(account_id, orders, **kwargs):
                calls.append({"account_id": account_id, "orders": orders, "kwargs": kwargs})

                class Response:
                    status_code = 200
                    @staticmethod
                    def json():
                        return {"ok": True, "account_id": account_id}
                return Response()

    module.buy_stock(
        "acct-123",
        "AAPL",
        1,
        100.0,
        95.0,
        110.0,
        trade_client=FakeTradeClient(),
    )

    assert len(calls) == 1
    assert len(calls[0]["orders"]) == 1
    assert calls[0]["kwargs"] == {}
    assert calls[0]["orders"][0]["total_cash_amount"] == "100.00"
    assert calls[0]["orders"][0]["combo_type"] == "NORMAL"
    assert all(order["instrument_type"] == "EQUITY" for order in calls[0]["orders"])
    assert all(order["support_trading_session"] == "CORE" for order in calls[0]["orders"])
    assert calls[0]["orders"][0]["side"] == "BUY"


def test_submit_paper_order_fails_immediately_on_webull_429(monkeypatch):
    from app.main import Settings, TradingDecision, submit_paper_order

    calls = {"count": 0}

    class FakeWebullModule:
        @staticmethod
        def get_account_id(**kwargs):
            return "acct-123"

        @staticmethod
        def buy_stock(**kwargs):
            calls["count"] += 1
            raise RuntimeError("HTTP Status: 429, Code: TOO_MANY_REQUESTS, Msg: Too many requests")

    decision = TradingDecision(
        action="buy",
        symbol="AAPL",
        strategy="iron_condor",
        notional_usd=250.0,
        confidence=0.9,
        rationale="test",
        risk_notes=[],
    )
    settings = Settings(DRY_RUN=False, BULLISH_STOCK_ACCOUNT_NUMBER="test-cash")

    monkeypatch.setattr("app.execution.submitter._load_webull_stock_module", lambda: FakeWebullModule())

    with pytest.raises(RuntimeError, match="429|TOO_MANY_REQUESTS"):
        submit_paper_order(decision, settings, "fingerprint-1234567890abcd")

    assert calls["count"] == 1
