import logging
from typing import Any, Dict, List, Tuple

import json
import os
from pathlib import Path

from dotenv import load_dotenv
import sys

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.common.paths import ENV_FILE
from app.config.webull import resolve_webull_endpoint
from webull.core.client import ApiClient
from webull.data.data_client import DataClient

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

# Prevent SDK from creating file log in project root; prefer stream logging.
api_client.set_stream_logger(stream=sys.stdout)

data_client = DataClient(api_client)

logger = logging.getLogger(__name__)


# ============================================================
# Helpers
# ============================================================

def _get_contract_expiry(contract: Dict[str, Any]) -> str | None:
    """
    Handle possible Webull SDK / response field-name variations.
    """
    for key in (
        "expiration_date",
        "expire_date",
        "expireDate",
        "option_expire_date",
        "option_expire_date",
        "init_expiration_date",
        "init_exp_date",
        "expiry",
    ):
        value = contract.get(key)
        if value:
            return str(value)
    return None


def _get_contract_type(contract: Dict[str, Any]) -> str:
    """
    Return normalized CALL / PUT.
    """
    for key in ("option_type", "optionType", "contract_type", "type"):
        value = contract.get(key)
        if value is not None:
            return str(value).upper().replace("OPTION", "").strip()
    return ""


def _extract_contracts(response: Any) -> List[Dict[str, Any]]:
    """
    Normalize different Webull SDK response shapes into a list of contracts.
    """

    # Some SDK calls return requests.Response.
    if hasattr(response, "status_code"):

        if response.status_code != 200:
            raise RuntimeError(
                f"Webull option contract request failed. "
                f"status={response.status_code}, "
                f"body={getattr(response, 'text', '')}"
            )

        response = response.json()

    # Direct list response.
    if isinstance(response, list):
        return response

    if not isinstance(response, dict):
        return []

    # Common response structures.
    candidates = [
        response.get("data"),
        response.get("items"),
        response.get("list"),
        response.get("contracts"),
    ]

    for candidate in candidates:

        if isinstance(candidate, list):
            return candidate

        if isinstance(candidate, dict):

            for key in (
                "items",
                "list",
                "contracts",
                "data",
            ):
                nested = candidate.get(key)

                if isinstance(nested, list):
                    return nested

    return []


# ============================================================
# Webull Option Chain
# ============================================================

def get_option_chain(
    client,
    symbol: str,
    option_type: str | None = None,
) -> List[Dict[str, Any]]:
    """
    Fetch all available Webull option contracts for an underlying.

    Example:
        contracts = get_option_chain(data_client, "MSFT")
    """

    symbol = symbol.upper().strip()

    logger.info(
        "Fetching Webull option contracts for %s",
        symbol,
    )

    try:
        instrument_client = getattr(client, "instrument", None)
        if instrument_client is None:
            raise AttributeError(
                "Unable to locate instrument API on Webull DataClient. "
                "Use DataClient(api_client) instead of TradeClient."
            )

        if hasattr(instrument_client, "get_option_contracts"):
            response = instrument_client.get_option_contracts(
                category="US_OPTION",
                underlying_symbols=symbol,
                option_type=option_type,
                page_size=100,
            )
        elif hasattr(instrument_client, "list_option_contracts"):
            response = instrument_client.list_option_contracts(
                symbol=symbol,
            )
        else:
            raise AttributeError(
                "Your Webull DataClient.instrument does not expose "
                "get_option_contracts() or list_option_contracts()."
            )

        print("\nRAW WEBULL API RESPONSE:")
        if hasattr(response, "text"):
            print(response.text[:4000])
        else:
            print(json.dumps(response, indent=2, default=str)[:4000])

        contracts = _extract_contracts(response)

        logger.info(
            "Webull returned %d option contracts for %s",
            len(contracts),
            symbol,
        )

        return contracts

    except Exception:
        logger.exception(
            "Failed retrieving Webull option contracts for %s",
            symbol,
        )
        raise


# ============================================================
# Expiration Extraction
# ============================================================

def get_available_expirations(
    contracts: List[Dict[str, Any]],
) -> List[str]:

    expirations = {
        expiry
        for contract in contracts
        if (expiry := _get_contract_expiry(contract))
    }

    expirations = sorted(expirations)

    return expirations


def print_all_expirations(symbol: str, contracts: List[Dict[str, Any]]) -> None:
    expirations = get_available_expirations(contracts)
    print(f"\nAvailable expirations for {symbol}:")
    if not expirations:
        print("  (none)")
        return
    for expiry in expirations:
        print(f"  - {expiry}")


# ============================================================
# Expiration Resolver
# ============================================================

from app.options.expiration import resolve_option_expiry


# ============================================================
# Filter CALL / PUT + Expiration
# ============================================================

def filter_option_contracts(
    contracts: List[Dict[str, Any]],
    option_type: str,
    expiry: str,
) -> List[Dict[str, Any]]:

    option_type = option_type.upper()

    filtered = []

    for contract in contracts:

        contract_expiry = _get_contract_expiry(contract)
        contract_type = _get_contract_type(contract)

        if (
            contract_expiry == expiry
            and contract_type == option_type
        ):
            filtered.append(contract)

    return filtered


# ============================================================
# Main Resolver
# ============================================================

def get_valid_webull_option_chain(
    trade_client,
    symbol: str,
    option_type: str,
    requested_expiry: str,
) -> Tuple[str, List[Dict[str, Any]]]:
    """
    Complete Webull option chain resolution.

    Returns:

        selected_expiry,
        matching_contracts
    """

    symbol = symbol.upper().strip()
    option_type = option_type.upper().strip()

    if option_type not in {"CALL", "PUT"}:
        raise ValueError(
            f"Invalid option_type={option_type}. "
            f"Expected CALL or PUT."
        )

    # --------------------------------------------------------
    # STEP 1
    # Fetch all contracts
    # --------------------------------------------------------

    contracts = get_option_chain(
        client=trade_client,
        symbol=symbol,
        option_type=option_type,
    )

    if not contracts:
        raise ValueError(
            f"No Webull option contracts returned for {symbol}."
        )

    # --------------------------------------------------------
    # STEP 2
    # Get available expirations
    # --------------------------------------------------------

    available_expiries = get_available_expirations(
        [c for c in contracts if _get_contract_type(c) == option_type]
    )

    logger.info(
        "Available Webull expirations for %s: %s",
        symbol,
        available_expiries,
    )

    if not available_expiries:
        raise ValueError(
            f"No Webull expiration dates found for {symbol}."
        )

    # --------------------------------------------------------
    # STEP 3
    # Resolve expiration
    # --------------------------------------------------------

    selected_expiry = resolve_option_expiry(
        requested_expiry=requested_expiry,
        available_expiries=available_expiries,
    )

    # --------------------------------------------------------
    # STEP 4
    # Filter CALL / PUT contracts
    # --------------------------------------------------------

    matching_contracts = filter_option_contracts(
        contracts=contracts,
        option_type=option_type,
        expiry=selected_expiry,
    )

    if not matching_contracts:
        raise ValueError(
            f"No Webull {symbol} {option_type} contracts "
            f"found for resolved expiry {selected_expiry}."
        )

    logger.info(
        "Resolved Webull option chain: "
        "symbol=%s type=%s "
        "requested_expiry=%s "
        "selected_expiry=%s "
        "contracts=%d",
        symbol,
        option_type,
        requested_expiry,
        selected_expiry,
        len(matching_contracts),
    )

    return selected_expiry, matching_contracts


# ============================================================
# Strike Selection
# ============================================================

def get_contract_strike(
    contract: Dict[str, Any],
) -> float | None:

    strike = (
        contract.get("strike_price")
        or contract.get("strikePrice")
        or contract.get("strike")
    )

    if strike is None:
        return None

    try:
        return float(strike)
    except (ValueError, TypeError):
        return None


def find_nearest_strike_contract(
    contracts: List[Dict[str, Any]],
    requested_strike: float,
) -> Dict[str, Any]:
    """
    Find closest strike to Optionomics requested strike.
    """

    valid_contracts = []

    for contract in contracts:

        strike = get_contract_strike(contract)

        if strike is not None:
            valid_contracts.append(
                (contract, strike)
            )

    if not valid_contracts:
        raise ValueError(
            "No contracts contained a valid strike price."
        )

    contract, selected_strike = min(
        valid_contracts,
        key=lambda item: abs(
            item[1] - requested_strike
        ),
    )

    logger.info(
        "Requested strike %.2f -> selected Webull strike %.2f",
        requested_strike,
        selected_strike,
    )

    return contract


# ============================================================
# Complete Contract Resolver
# ============================================================

def resolve_webull_option_contract(
    trade_client,
    symbol: str,
    option_type: str,
    requested_expiry: str,
    requested_strike: float,
) -> Dict[str, Any]:
    """
    Resolve BOTH expiration and strike.

    Example:

        Optionomics:
            MSFT
            CALL
            2026-09-03
            $510

        Webull:
            expiration -> 2026-09-04
            strike -> $510

        Returns selected Webull contract.
    """

    selected_expiry, contracts = (
        get_valid_webull_option_chain(
            trade_client=trade_client,
            symbol=symbol,
            option_type=option_type,
            requested_expiry=requested_expiry,
        )
    )

    selected_contract = (
        find_nearest_strike_contract(
            contracts=contracts,
            requested_strike=float(
                requested_strike
            ),
        )
    )

    logger.info("selected_contract=%s", json.dumps(selected_contract, indent=2))

    selected_strike = get_contract_strike(
        selected_contract
    )

    logger.info(
        "FINAL WEBULL CONTRACT: "
        "symbol=%s "
        "type=%s "
        "requested_expiry=%s "
        "selected_expiry=%s "
        "requested_strike=%s "
        "selected_strike=%s",
        symbol,
        option_type,
        requested_expiry,
        selected_expiry,
        requested_strike,
        selected_strike,
    )

    return selected_contract


# ============================================================
# TEST
# ============================================================

if __name__ == "__main__":

    # --------------------------------------------------------
    # trade_client should already be initialized by your
    # existing application.
    #
    # Example:
    #
    # trade_client = ...
    # --------------------------------------------------------

    symbol = "MSFT"
    option_type = "CALL"

    # Optionomics requested expiration
    requested_expiry = "2026-08-28"

    # Example strike
    requested_strike = 510.00

    try:

        contracts = get_option_chain(
            client=data_client,
            symbol=symbol,
        )
        print_all_expirations(symbol, contracts)

        selected_contract = (
            resolve_webull_option_contract(
                trade_client=data_client,
                symbol=symbol,
                option_type=option_type,
                requested_expiry=requested_expiry,
                requested_strike=requested_strike,
            )
        )

        print("\n==============================")
        print("SELECTED WEBULL CONTRACT")
        print("==============================")

        print(
            "Symbol:",
            symbol,
        )

        print(
            "Option type:",
            option_type,
        )

        print(
            "Expiry:",
            _get_contract_expiry(
                selected_contract
            ),
        )

        print(
            "Strike:",
            get_contract_strike(
                selected_contract
            ),
        )

        print(
            "Contract:",
            selected_contract,
        )

    except Exception as exc:

        logger.exception(
            "Unable to resolve Webull option contract: %s",
            exc,
        )
