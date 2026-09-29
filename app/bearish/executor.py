from __future__ import annotations

from typing import Any


class BearishPutOptionExecutor:
    """Executes bearish directional option ideas with Webull PUT bracket orders."""

    def __init__(self, module: Any | None = None):
        self.module = module or self._load_option_module()

    @staticmethod
    def _load_option_module() -> Any:
        from importlib import import_module
        return import_module('app.options.brackets')

    @staticmethod
    def option_type() -> str:
        return "PUT"

    @staticmethod
    def strategy_label() -> str:
        return "sell_next_way"

    @staticmethod
    def order_builder_name() -> str:
        return "buy_put_with_bracket"

    def submit(
        self,
        *,
        account_id: str,
        symbol: str,
        strike: float,
        expiration: str | None,
        quantity: int,
        entry_limit: float | None = None,
        profit_percent: float = 20,
        stop_loss_percent: float = 10,
        quote_max_age_seconds: int = 60,
        stop_loss_enabled: bool = True,
    ) -> dict[str, Any]:
        builder = getattr(self.module, self.order_builder_name())
        return builder(
            account_id=account_id,
            symbol=symbol,
            strike=strike,
            expiration=expiration,
            quantity=quantity,
            entry_limit=entry_limit,
            profit_percent=profit_percent,
            stop_loss_percent=stop_loss_percent,
            quote_max_age_seconds=quote_max_age_seconds,
            **({"stop_loss_enabled": False} if not stop_loss_enabled else {}),
        )
