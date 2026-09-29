from app.common.paths import ENV_FILE
import os
import uuid
import json

from dotenv import load_dotenv
import sys
from app.config.webull import resolve_webull_endpoint
from webull.core.client import ApiClient
from webull.trade.trade_client import TradeClient

load_dotenv(ENV_FILE)

# ==========================================================
# CONFIGURATION
# ==========================================================

APP_KEY = os.environ["WEBULL_APP_KEY"]
APP_SECRET = os.environ["WEBULL_APP_SECRET"]

# Selected Webull trading endpoint
WEBULL_ENDPOINT = resolve_webull_endpoint()


# ==========================================================
# CREATE WEBULL CLIENT
# ==========================================================

api_client = ApiClient(
    APP_KEY,
    APP_SECRET,
    "us"
)

api_client.add_endpoint(
    "us",
    WEBULL_ENDPOINT
)

# Prevent SDK from creating file logs in the current working directory.
api_client.set_stream_logger(stream=sys.stdout)

trade_client = TradeClient(api_client)


# ==========================================================
# GET PAPER TRADING ACCOUNT ID
# ==========================================================

def get_account_id():
    response = trade_client.account_v2.get_account_list()

    if response.status_code != 200:
        raise RuntimeError(
            f"Unable to retrieve account: "
            f"{response.status_code} {response.text}"
        )

    accounts = response.json()

    if not accounts:
        raise RuntimeError("No Webull account found.")

    print("Accounts:")
    print(json.dumps(accounts, indent=2))

    return accounts[0]["account_id"]


# ==========================================================
# BUY CALL OPTION - LIMIT ORDER
# ==========================================================

def buy_call_limit(
    account_id: str,
    symbol: str,
    strike_price: float,
    expiration: str,
    quantity: int,
    limit_price: float
):
    """
    Buy a CALL option using a LIMIT order.

    Example:
        AAPL 220 CALL
        Expiration: 2026-09-18
        Quantity: 1
        Limit price: $5.25
    """

    client_order_id = uuid.uuid4().hex

    order = {
        "client_order_id": client_order_id,

        # Normal single order
        "combo_type": "NORMAL",

        # Underlying
        "symbol": symbol.upper(),

        "instrument_type": "OPTION",
        "market": "US",

        # Single-leg option
        "option_strategy": "SINGLE",

        # BUY call
        "side": "BUY",

        # LIMIT order
        "order_type": "LIMIT",
        "limit_price": f"{limit_price:.2f}",

        # Number of option contracts
        "quantity": str(quantity),

        "time_in_force": "DAY",
        "entrust_type": "QTY",

        # Option contract details
        "legs": [
            {
                "side": "BUY",

                "quantity": str(quantity),

                "symbol": symbol.upper(),

                "strike_price": f"{strike_price:.2f}",

                "option_expire_date": expiration,

                "instrument_type": "OPTION",

                "option_type": "CALL",

                "market": "US"
            }
        ]
    }

    print("\nSubmitting Webull option order:")
    print(json.dumps(order, indent=2))

    from decimal import Decimal
    from app.execution.daily_budget import reserve_live_option_budget
    reserve_live_option_budget(Decimal(order['limit_price']) * quantity * 100, order['client_order_id'])
    response = trade_client.order_v3.place_order(
        account_id,
        [order]
    )

    if response.status_code == 200:
        result = response.json()

        print("\nORDER SUBMITTED")
        print(json.dumps(result, indent=2))

        return result

    raise RuntimeError(
        f"Order rejected: "
        f"{response.status_code} {response.text}"
    )


# ==========================================================
# RUN
# ==========================================================

if __name__ == "__main__":

    account_id = get_account_id()

    print("Using account:", account_id)

    result = buy_call_limit(

        account_id=account_id,

        # Underlying
        symbol="AAPL",

        # AAPL $220 Call
        strike_price=220,

        # YYYY-MM-DD
        expiration="2026-09-18",

        # Buy 1 contract
        quantity=1,

        # Pay maximum $5.25/share
        limit_price=5.25
    )
