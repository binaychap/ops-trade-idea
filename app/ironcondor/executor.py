"""Quote-priced, defined-risk credit iron-condor brackets for Webull paper trading."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
from typing import Any
from app.config.strategy import iron_condor_enabled

from app.broker.client import get_data_client, get_trade_client, new_id
from app.broker.quotes import QuoteError


class CondorValidationError(ValueError):
    """A setup cannot safely be priced or submitted."""


def number(value):
    if isinstance(value, bool):
        raise CondorValidationError("Boolean numeric value")
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise CondorValidationError("Invalid numeric value") from exc
    if not result.is_finite():
        raise CondorValidationError("Nonfinite numeric value")
    return result


def tick_price(value, rounding=ROUND_HALF_UP):
    tick = Decimal("0.05")
    return (value / tick).quantize(Decimal("1"), rounding=rounding) * tick


def select_contracts(data_client, symbol, expiration, reference, width):
    """Select four standard listed contracts at the first usable expiry in 14 days."""
    start = date.fromisoformat(expiration)
    end = start + timedelta(days=14)
    groups = {}
    cursor = None
    seen_cursors = set()
    for _ in range(20):
        kwargs = dict(category="US_OPTION", underlying_symbols=symbol,
                      start_date=start.isoformat(), end_date=end.isoformat(), page_size=500)
        if cursor:
            kwargs["last_instrument_id"] = cursor
        response = data_client.instrument.get_option_contracts(**kwargs)
        if response.status_code != 200:
            raise CondorValidationError(f"Contract lookup failed (HTTP {response.status_code})")
        payload = response.json()
        rows = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            raise CondorValidationError("Unexpected option contract response")
        for row in rows:
            if not isinstance(row, dict):
                continue
            kind = row.get("option_type")
            exp = row.get("expiration_date") or row.get("expiration")
            try:
                expiry = date.fromisoformat(exp)
                strike = number(row.get("strike_price", row.get("strike")))
                multiplier = number(row.get("multiplier"))
            except (ValueError, TypeError):
                continue
            if kind not in {"PUT", "CALL"} or not start <= expiry <= end or strike <= 0 or multiplier != 100:
                continue
            # Standard OCC identity prevents mixing adjusted roots or other underlyings.
            expected = f"{symbol}{expiry:%y%m%d}{kind[0]}{int(strike * 1000):08d}"
            contract_symbol = row.get("option_symbol") or row.get("symbol")
            if contract_symbol != expected or strike * 1000 != int(strike * 1000):
                continue
            key = (exp, kind, strike)
            previous = groups.get(key)
            if previous and previous["symbol"] != contract_symbol:
                raise CondorValidationError("Ambiguous option contract")
            groups[key] = {"symbol": contract_symbol, "expiration": exp,
                           "strike": strike, "option_type": kind}
        if len(rows) < 500:
            break
        cursor = rows[-1].get("instrument_id")
        if not cursor or cursor in seen_cursors:
            raise CondorValidationError("Incomplete option contract pagination")
        seen_cursors.add(cursor)
    else:
        raise CondorValidationError("Option contract pagination limit exceeded")

    for exp in sorted({key[0] for key in groups}):
        puts = sorted(key[2] for key in groups if key[:2] == (exp, "PUT") and key[2] < reference)
        calls = sorted(key[2] for key in groups if key[:2] == (exp, "CALL") and key[2] > reference)
        if len(puts) < 2 or len(calls) < 2:
            continue
        short_put = min(puts[1:], key=lambda s: abs(s - (reference - width)))
        short_call = min(calls[:-1], key=lambda s: abs(s - (reference + width)))
        # Equal wing widths preserve a standard iron-condor shape.
        put_widths = {short_put - s for s in puts if s < short_put}
        call_widths = {s - short_call for s in calls if s > short_call}
        widths = put_widths & call_widths
        if not widths:
            continue
        wing = min(widths, key=lambda w: (abs(w - width), w))
        selected = [groups[(exp, "PUT", short_put - wing)], groups[(exp, "PUT", short_put)],
                    groups[(exp, "CALL", short_call)], groups[(exp, "CALL", short_call + wing)]]
        return selected, wing
    raise CondorValidationError("No standard four-leg iron condor with a common listed expiry and equal wings")


def quote_legs(data_client, contracts, now, *, max_age_seconds=60):
    symbols = [c["symbol"] for c in contracts]
    response = data_client.option_market_data.get_option_snapshot(symbols, "US_OPTION")
    if response.status_code != 200:
        raise QuoteError(f"Option snapshot request failed (HTTP {response.status_code})")
    rows = response.json()
    now = now or datetime.now(UTC)
    if not isinstance(rows, list):
        raise QuoteError("Option snapshot must be a list")
    quotes = []
    for symbol in symbols:
        matches = [r for r in rows if isinstance(r, dict) and r.get("symbol") == symbol]
        if len(matches) != 1:
            raise QuoteError("Missing or ambiguous iron-condor quote")
        row = matches[0]
        bid, ask = number(row.get("bid")), number(row.get("ask"))
        age = number(now.timestamp()) - number(row.get("quote_time")) / 1000
        if not 0 < bid <= ask:
            raise QuoteError(f"Invalid or crossed iron-condor quote for {symbol}")
        if not -5 <= age <= max_age_seconds:
            raise QuoteError(
                f"Iron-condor quote for {symbol} has age {age:.1f} seconds "
                f"(maximum {max_age_seconds} seconds; future tolerance 5 seconds)"
            )
        quotes.append({"symbol": symbol, "bid": str(bid), "ask": str(ask), "quote_time": row["quote_time"]})
    return quotes


class IronCondorOptionExecutor:
    def __init__(self, module: Any | None = None, *, data_client=None):
        self.module = module
        self.data_client = data_client

    @staticmethod
    def option_type():
        return "IRON_CONDOR"

    @staticmethod
    def strategy_label():
        return "iron_condor"

    def submit(self, *, account_id, symbol, expiration, reference_price, max_risk_usd,
               quantity=1, wing_width=None, trade_client=None, profit_percent=10,
               stop_loss_percent=5, exit_time_in_force="GTC", before_submit=None, now=None,
               quote_max_age_seconds=60):
        if not iron_condor_enabled():
            raise CondorValidationError("Iron-condor trading is disabled in live mode")
        requested_now = now
        now = now or datetime.now(UTC)
        ref, budget = number(reference_price), number(max_risk_usd)
        profit, loss = number(profit_percent), number(stop_loss_percent)
        if ref <= 0 or budget <= 0 or not 0 < profit < 100 or loss <= 0:
            raise CondorValidationError("Invalid reference, risk budget or exit percentages")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            raise CondorValidationError("Quantity must be a positive integer")
        if exit_time_in_force not in {"DAY", "GTC"}:
            raise CondorValidationError("Invalid exit duration")
        if date.fromisoformat(expiration) <= now.date():
            raise CondorValidationError("Expiry must be in the future")
        symbol = symbol.strip().upper()
        width = number(wing_width) if wing_width is not None else max(Decimal(1), ref * Decimal("0.05"))
        if width <= 0:
            raise CondorValidationError("Wing width must be positive")
        data = self.data_client or get_data_client()
        contracts, wing = select_contracts(data, symbol, expiration, ref, width)
        quotes = quote_legs(data, contracts, requested_now, max_age_seconds=quote_max_age_seconds)
        # Sell inner legs at bid, buy protection at ask; no synthetic premium estimate.
        raw_credit = number(quotes[1]["bid"]) + number(quotes[2]["bid"]) - number(quotes[0]["ask"]) - number(quotes[3]["ask"])
        credit = tick_price(raw_credit, ROUND_FLOOR)
        if not 0 < credit < wing:
            raise CondorValidationError("Nonpositive or implausible net credit")
        max_loss = (wing - credit) * 100 * quantity
        if max_loss > budget:
            raise CondorValidationError(f"Iron-condor maximum spread loss {max_loss:.2f} exceeds budget {budget:.2f}")
        take_profit = tick_price(credit * (1 - profit / 100))
        stop = tick_price(credit * (1 + loss / 100))
        if not 0 < take_profit < credit < stop:
            raise CondorValidationError("Exit prices collapse after tick rounding")

        def leg(contract, side):
            return {"side": side, "quantity": str(quantity), "symbol": symbol,
                    "strike_price": f"{contract['strike']:.2f}", "option_expire_date": contract["expiration"],
                    "instrument_type": "OPTION", "option_type": contract["option_type"], "market": "US"}

        sides = ["BUY", "SELL", "SELL", "BUY"]
        entry_legs = [leg(c, s) for c, s in zip(contracts, sides)]
        exit_legs = [leg(c, "SELL" if s == "BUY" else "BUY") for c, s in zip(contracts, sides)]
        common = {"option_strategy": "IRON_CONDOR", "instrument_type": "OPTION", "market": "US",
                  "symbol": symbol, "quantity": str(quantity), "entrust_type": "QTY"}
        orders = [
            {**common, "client_order_id": new_id(), "combo_type": "MASTER", "order_type": "LIMIT",
             "side": "SELL", "position_intent": "SELL_TO_OPEN", "time_in_force": "DAY",
             "limit_price": f"{credit:.2f}", "legs": entry_legs},
            {**common, "client_order_id": new_id(), "combo_type": "STOP_PROFIT", "order_type": "LIMIT",
             "side": "BUY", "time_in_force": exit_time_in_force,
             "limit_price": f"{take_profit:.2f}", "legs": exit_legs},
            {**common, "client_order_id": new_id(), "combo_type": "STOP_LOSS", "order_type": "STOP_LOSS",
             "side": "BUY", "time_in_force": exit_time_in_force,
             "stop_price": f"{stop:.2f}", "legs": [dict(leg) for leg in exit_legs]},
        ]
        plan = {"combo_id": new_id(), "entry_id": orders[0]["client_order_id"],
                "profit_id": orders[1]["client_order_id"], "stop_id": orders[2]["client_order_id"],
                "account_id": account_id, "orders": orders, "quotes": quotes,
                "entry_credit": float(credit), "profit_debit": float(take_profit),
                "stop_debit": float(stop), "max_loss_usd": float(max_loss)}
        client = trade_client or (self.module.get_trade_client() if self.module else get_trade_client())
        from app.execution.daily_budget import reserve_live_option_budget
        reserve_live_option_budget(max_loss, plan['entry_id'])
        if before_submit:
            before_submit(plan)
        response = client.order_v3.place_order(account_id, orders, client_combo_order_id=plan["combo_id"])
        if response.status_code != 200:
            raise RuntimeError(f"Iron-condor submission failed (HTTP {response.status_code}); reconcile saved IDs before retrying")
        result = response.json()
        if not isinstance(result, dict):
            raise RuntimeError("Unexpected iron-condor submission response; reconcile saved IDs")
        return {**plan, "broker_response": result}
