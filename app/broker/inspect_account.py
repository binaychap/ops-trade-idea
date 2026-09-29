from app.common.paths import ENV_FILE
import json
import os

from dotenv import load_dotenv
from app.config.webull import resolve_webull_endpoint
from webull.core.client import ApiClient
from webull.trade.trade_client import TradeClient
import sys

load_dotenv(ENV_FILE)

APP_KEY = os.getenv("WEBULL_APP_KEY")
APP_SECRET = os.getenv("WEBULL_APP_SECRET")

if not APP_KEY or not APP_SECRET:
    raise RuntimeError(
        "WEBULL_APP_KEY and WEBULL_APP_SECRET must be set in the environment or .env file."
    )

api_client = ApiClient(
    APP_KEY,
    APP_SECRET,
    "us"
)

# Selected paper/live environment
api_client.add_endpoint(
    "us",
    resolve_webull_endpoint()
)

# Ensure the SDK doesn't create a local file logger in the cwd; use stream logger instead.
api_client.set_stream_logger(stream=sys.stdout)

trade_client = TradeClient(api_client)

# Verify paper account
response = trade_client.account_v2.get_account_list()

if response.status_code == 200:
    print(json.dumps(response.json(), indent=2))
else:
    print("Error:", response.status_code, response.text)
