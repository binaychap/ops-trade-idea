from app.persistence.ledger import Ledger
from app.bearish.lifecycle import BearishPutLifecycle
from types import SimpleNamespace
from webull.core.exception.exceptions import ServerException


def test_bearish_option_jobs_are_unique_and_resumable(tmp_path):
    ledger = Ledger(str(tmp_path / "trades.sqlite3"))
    job = {
        "status": "entry_pending",
        "entry_client_order_id": "entry-1",
        "symbol": "AAPL",
    }

    assert ledger.register_bearish_option_job("job-1", job)
    assert not ledger.register_bearish_option_job("job-1", job)
    assert ledger.bearish_option_job("job-1") == job
    assert ledger.bearish_option_jobs() == [job]

    job["status"] = "exits_active"
    ledger.save_bearish_option_job("job-1", job)

    assert ledger.bearish_option_job("job-1") == job
    assert ledger.bearish_option_jobs() == []


def test_market_put_fill_attaches_fill_based_broker_exits(tmp_path):
    ledger = Ledger(str(tmp_path / "trades.sqlite3"))
    calls = []

    class FakeOrderApi:
        def place_order(self, account_id, orders, **kwargs):
            calls.append((account_id, orders, kwargs))
            if orders[0]["order_type"] == "MARKET":
                return SimpleNamespace(status_code=200, json=lambda: {
                    "orders": [{
                        "client_order_id": orders[0]["client_order_id"],
                        "order_id": "broker-entry-1",
                    }],
                })
            return SimpleNamespace(status_code=200, json=lambda: {"orders": []})

        def get_order_detail(self, account_id, order_id):
            return SimpleNamespace(status_code=200, json=lambda: {
                "orders": [{
                    "client_order_id": order_id,
                    "order_id": "broker-entry-1",
                    "symbol": "CTVA",
                    "side": "BUY",
                    "instrument_type": "OPTION",
                    "status": "FILLED",
                    "total_quantity": "1",
                    "filled_quantity": "1",
                    "filled_price": "2.13",
                }],
            })

        def cancel_order(self, account_id, order_id):
            raise AssertionError("Fully filled entry must not be cancelled")

    client = SimpleNamespace(order_v3=FakeOrderApi())
    option_module = SimpleNamespace(
        _find_valid_contract=lambda *args, **kwargs: (
            "2026-10-16", 10.0, "CTVA261016P00010000",
        ),
        round_to_tick=__import__("app.options.brackets", fromlist=["round_to_tick"]).round_to_tick,
    )
    lifecycle = BearishPutLifecycle(ledger, trade_client=client, option_module=option_module)
    job = lifecycle.submit_entry(
        job_id="job-market-put",
        trade_id="trade-1",
        account_id="account-1",
        symbol="CTVA",
        desired_strike=10,
        quantity=1,
        profit_percent=20,
        stop_loss_percent=10,
        stop_loss_enabled=True,
    )

    assert calls[0][1][0]["order_type"] == "MARKET"
    assert "limit_price" not in calls[0][1][0]
    lifecycle.reconcile(job)

    exit_account, exits, exit_kwargs = calls[1]
    assert exit_account == "account-1"
    assert [order["combo_type"] for order in exits] == ["STOP_PROFIT", "STOP_LOSS"]
    assert exits[0]["order_type"] == "LIMIT"
    assert exits[0]["limit_price"] == "2.55"
    assert exits[1]["order_type"] == "STOP_LOSS"
    assert exits[1]["stop_price"] == "1.90"
    assert all(order["time_in_force"] == "GTC" for order in exits)
    assert all(order["legs"][0]["option_expire_date"] == "2026-10-16" for order in exits)
    assert exit_kwargs["client_combo_order_id"] == job["exit_combo_order_id"]
    saved = ledger.bearish_option_job("job-market-put")
    assert saved["status"] == "exits_active"
    assert saved["fill_price"] == "2.13"


def test_ambiguous_market_entry_is_never_replayed(tmp_path):
    ledger = Ledger(str(tmp_path / "trades.sqlite3"))
    calls = []

    class FakeOrderApi:
        def place_order(self, account_id, orders, **kwargs):
            calls.append(orders)
            raise TimeoutError("response timed out")

    client = SimpleNamespace(order_v3=FakeOrderApi())
    module = SimpleNamespace(_find_valid_contract=lambda *args, **kwargs: (
        "2026-10-16", 10.0, "CTVA261016P00010000",
    ))
    lifecycle = BearishPutLifecycle(ledger, trade_client=client, option_module=module)
    args = dict(
        job_id="job-ambiguous-put",
        trade_id="trade-2",
        account_id="account-1",
        symbol="CTVA",
        desired_strike=10,
        quantity=1,
        profit_percent=20,
        stop_loss_percent=10,
        stop_loss_enabled=True,
    )

    first = lifecycle.submit_entry(**args)
    second = lifecycle.submit_entry(**args)

    assert first["status"] == "entry_submitting"
    assert second["entry_client_order_id"] == first["entry_client_order_id"]
    assert len(calls) == 1


def test_unsupported_market_order_is_terminal_and_not_replayed(tmp_path, caplog):
    ledger = Ledger(str(tmp_path / "trades.sqlite3"))
    calls = []

    class FakeOrderApi:
        def place_order(self, account_id, orders, **kwargs):
            calls.append(("place", orders))
            raise ServerException(
                "OPENAPI_OPTION_NOT_ALLOW_PLACING_MARKET_ORDER",
                "This contract has limited liquidity and does not support market orders.",
                http_status=417,
            )

        def get_order_detail(self, account_id, order_id):
            calls.append(("detail", order_id))
            raise AssertionError("Rejected order must not be reconciled")

    client = SimpleNamespace(order_v3=FakeOrderApi())
    module = SimpleNamespace(_find_valid_contract=lambda *args, **kwargs: (
        "2026-10-16", 10.0, "CTVA261016P00010000",
    ))
    lifecycle = BearishPutLifecycle(ledger, trade_client=client, option_module=module)
    args = dict(
        job_id="job-market-rejected",
        trade_id="trade-rejected",
        account_id="account-1",
        symbol="CTVA",
        desired_strike=10,
        quantity=1,
        profit_percent=20,
        stop_loss_percent=10,
        stop_loss_enabled=True,
    )

    first = lifecycle.submit_entry(**args)
    second = lifecycle.submit_entry(**args)
    lifecycle.reconcile(first)

    assert first["status"] == second["status"] == "entry_rejected"
    assert first["next_attempt_at"] is None
    assert first["rejection_code"] == "OPENAPI_OPTION_NOT_ALLOW_PLACING_MARKET_ORDER"
    assert "does not support market orders" in first["last_error"]
    assert calls[0][0] == "place"
    assert len(calls) == 1
    assert ledger.bearish_option_jobs() == []
    assert "Webull rejected bearish PUT market entry" in caplog.text


def test_stuck_market_rejection_is_repaired_from_saved_error(tmp_path):
    ledger = Ledger(str(tmp_path / "trades.sqlite3"))
    job = {
        "job_id": "old-job",
        "status": "entry_submitting",
        "trade_id": "old-trade",
        "symbol": "CTVA",
        "contract_symbol": "CTVA261016P00010000",
        "expiration": "2026-10-16",
        "last_error": (
            "HTTP Status: 417, Code: "
            "OPENAPI_OPTION_NOT_ALLOW_PLACING_MARKET_ORDER"
        ),
        "next_attempt_at": None,
    }
    assert ledger.register_bearish_option_job("old-job", job)
    lifecycle = BearishPutLifecycle(ledger, trade_client=object(), option_module=object())

    repaired = lifecycle.submit_entry(
        job_id="old-job",
        trade_id="old-trade",
        account_id="account-1",
        symbol="CTVA",
        desired_strike=10,
        quantity=1,
        profit_percent=20,
        stop_loss_percent=10,
        stop_loss_enabled=True,
    )

    assert repaired["status"] == "entry_rejected"
    assert repaired["rejection_code"] == "OPENAPI_OPTION_NOT_ALLOW_PLACING_MARKET_ORDER"
    assert ledger.bearish_option_jobs() == []


def test_submitter_returns_market_order_rejection_as_skipped(monkeypatch, tmp_path):
    from app.execution import submitter

    class FakeLifecycle:
        def __init__(self, ledger, *, option_module):
            pass

        def submit_entry(self, **kwargs):
            return {
                "status": "entry_rejected",
                "last_error": "HTTP Status: 417, Code: OPENAPI_OPTION_NOT_ALLOW_PLACING_MARKET_ORDER",
            }

    monkeypatch.setattr(
        submitter,
        "_load_webull_option_module",
        lambda: SimpleNamespace(get_account_id=lambda **kwargs: "account-1"),
    )
    monkeypatch.setattr("app.bearish.lifecycle.BearishPutLifecycle", FakeLifecycle)
    monkeypatch.setattr("app.bearish.lifecycle.start_bearish_put_reconciler", lambda path: None)
    settings = SimpleNamespace(
        dry_run=False,
        webull_trading_mode="paper",
        options_margin_account_number="test-margin",
        database_path=str(tmp_path / "trades.sqlite3"),
    )

    result = submitter.submit_paper_order(
        {"action": "sell_short", "symbol": "CTVA", "notional_usd": 250},
        settings,
        "fingerprint",
        SimpleNamespace(direction="bearish", entry_price=12.32),
    )

    assert result["skipped"] is True
    assert "OPENAPI_OPTION_NOT_ALLOW_PLACING_MARKET_ORDER" in result["reason"]


def test_filled_status_with_zero_quantity_is_deferred_not_marked_no_fill(tmp_path):
    ledger = Ledger(str(tmp_path / "trades.sqlite3"))

    class FakeOrderApi:
        def place_order(self, account_id, orders, **kwargs):
            return SimpleNamespace(status_code=200, json=lambda: {"orders": []})

        def get_order_detail(self, account_id, order_id):
            return SimpleNamespace(status_code=200, json=lambda: {
                "orders": [{
                    "client_order_id": order_id,
                    "symbol": "CTVA",
                    "side": "BUY",
                    "instrument_type": "OPTION",
                    "status": "FILLED",
                    "total_quantity": "1",
                    "filled_quantity": "0",
                    "filled_price": "0",
                }],
            })

    client = SimpleNamespace(order_v3=FakeOrderApi())
    module = SimpleNamespace(_find_valid_contract=lambda *args, **kwargs: (
        "2026-10-16", 10.0, "CTVA261016P00010000",
    ))
    lifecycle = BearishPutLifecycle(ledger, trade_client=client, option_module=module)
    job = lifecycle.submit_entry(
        job_id="job-invalid-fill",
        trade_id="trade-3",
        account_id="account-1",
        symbol="CTVA",
        desired_strike=10,
        quantity=1,
        profit_percent=20,
        stop_loss_percent=10,
        stop_loss_enabled=True,
    )

    lifecycle.reconcile(job)

    saved = ledger.bearish_option_job("job-invalid-fill")
    assert saved["status"] == "entry_pending"
    assert "FILLED with an incomplete quantity" in saved["last_error"]