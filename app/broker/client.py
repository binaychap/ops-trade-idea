from app.common.paths import ENV_FILE
"""Shared paper/live client, account lookup, and order identifiers."""

import logging
import uuid

from dotenv import load_dotenv
import sys
from app.config.webull import resolve_webull_endpoint, resolve_webull_credentials
from webull.core.client import ApiClient
from webull.trade.trade_client import TradeClient

load_dotenv(ENV_FILE)
# ============================================================
# WEBULL ENVIRONMENT
# ============================================================

_trade_client = None
_api_client = None
_data_client = None


class SafeSdkLogFilter(logging.Filter):
    def filter(self, record):
        # The SDK embeds signed request headers in ERROR messages as well as DEBUG.
        if record.name == 'webull.core.client' and record.levelno >= logging.ERROR:
            record.msg = 'Webull SDK request failed; see application error for status and reason.'
            record.args = ()
            record.exc_info = None
            record.exc_text = None
        return True


def get_api_client():
    global _api_client

    if _api_client is not None:
        return _api_client

    app_key, app_secret = resolve_webull_credentials()

    api_client = ApiClient(
        app_key,
        app_secret,
        "us"
    )

    api_client.add_endpoint(
        "us",
        resolve_webull_endpoint()
    )

    # Prevent the SDK from creating local file logs in the repository root.
    api_client.set_stream_logger(stream=sys.stdout, log_level=logging.INFO)
    sdk_logger = logging.getLogger('webull.core')
    sdk_logger.propagate = False
    for handler in sdk_logger.handlers:
        handler.setLevel(logging.INFO)
        handler.addFilter(SafeSdkLogFilter())

    _api_client = api_client
    return _api_client


def get_trade_client():
    global _trade_client
    if _trade_client is None:
        _trade_client = TradeClient(get_api_client())
    return _trade_client


def get_data_client():
    from webull.data.data_client import DataClient

    global _data_client
    if _data_client is None:
        _data_client = DataClient(get_api_client())
    return _data_client


# ============================================================
# ACCOUNT
# ============================================================

def get_account_id(*, account_number=None):
    response = get_trade_client().account_v2.get_account_list()

    if response.status_code != 200:
        raise RuntimeError(
            f"Account lookup failed: "
            f"{response.status_code} {response.text}"
        )

    accounts = response.json()

    if account_number is not None:
        matches = [a for a in accounts if a.get('account_number') == account_number]
        if not account_number or len(matches) != 1 or not matches[0].get('account_id'):
            raise ValueError('Configured Webull account number must match exactly one available account')
        return matches[0]['account_id']

    if not accounts:
        raise RuntimeError("No Webull account found")

    return accounts[0]["account_id"]


# ============================================================
# UNIQUE ID
# Webull client_order_id max = 32 chars
# ============================================================

def new_id():
    return uuid.uuid4().hex[:32]
