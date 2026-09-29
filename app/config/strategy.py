"""Shared strategy exit percentages; values are percentages, not fractions."""
from pydantic import Field, model_validator
from app.config.webull import WebullSettings


class StrategyExitSettings(WebullSettings):
    live_bullish_amount_usd: float = Field(default=100, ge=5, allow_inf_nan=False, alias="WEBULL_LIVE_BULLISH_AMOUNT_USD")
    top_bullish_account_number: str = Field(default="", alias="TOP_BULLISH_ACCOUNT_NUMBER", repr=False)
    paper_bullish_stock_account_number: str = Field(default="", alias="WEBULL_PAPER_BULLISH_STOCK_ACCOUNT_NUMBER", repr=False)
    paper_top_bullish_account_number: str = Field(default="", alias="WEBULL_PAPER_TOP_BULLISH_ACCOUNT_NUMBER", repr=False)
    paper_options_margin_account_number: str = Field(default="", alias="WEBULL_PAPER_OPTIONS_MARGIN_ACCOUNT_NUMBER", repr=False)
    live_bullish_stock_account_number: str = Field(default="", alias="WEBULL_LIVE_BULLISH_STOCK_ACCOUNT_NUMBER", repr=False)
    live_top_bullish_account_number: str = Field(default="", alias="WEBULL_LIVE_TOP_BULLISH_ACCOUNT_NUMBER", repr=False)
    live_options_margin_account_number: str = Field(default="", alias="WEBULL_LIVE_OPTIONS_MARGIN_ACCOUNT_NUMBER", repr=False)

    iron_condor_quote_max_age_seconds: int = Field(default=60, gt=0, alias="IRON_CONDOR_QUOTE_MAX_AGE_SECONDS")
    bearish_quote_max_age_seconds: int = Field(default=60, gt=0, alias="BEARISH_QUOTE_MAX_AGE_SECONDS")
    bullish_stock_account_number: str = Field(default="", alias="BULLISH_STOCK_ACCOUNT_NUMBER", repr=False)
    options_margin_account_number: str = Field(default="", alias="OPTIONS_MARGIN_ACCOUNT_NUMBER", repr=False)
    bullish_profit_percent: float = Field(default=10, gt=0, allow_inf_nan=False, alias='BULLISH_PROFIT_PERCENT')
    bullish_stop_loss_percent: float = Field(default=5, gt=0, lt=100, allow_inf_nan=False, alias='BULLISH_STOP_LOSS_PERCENT')
    bearish_profit_percent: float = Field(default=20, gt=0, allow_inf_nan=False, alias='BEARISH_PROFIT_PERCENT')
    bearish_stop_loss_percent: float = Field(default=10, gt=0, lt=100, allow_inf_nan=False, alias='BEARISH_STOP_LOSS_PERCENT')
    iron_condor_profit_percent: float = Field(default=10, gt=0, lt=100, allow_inf_nan=False, alias='IRON_CONDOR_PROFIT_PERCENT')
    iron_condor_stop_loss_percent: float = Field(default=5, gt=0, allow_inf_nan=False, alias='IRON_CONDOR_STOP_LOSS_PERCENT')


    @model_validator(mode="after")
    def select_environment_accounts(self):
        for role in ('bullish_stock', 'top_bullish', 'options_margin'):
            target = f'{role}_account_number'
            selected = getattr(self, f'{self.webull_trading_mode}_{target}').strip()
            # Legacy account fields remain paper-only for existing deployments.
            if self.webull_trading_mode == 'paper' and not selected:
                selected = getattr(self, target).strip()
                if role == 'top_bullish' and hasattr(self, 'account_number'):
                    selected = self.account_number.strip() or selected
            setattr(self, target, selected)
        if hasattr(self, 'account_number'):
            self.account_number = self.top_bullish_account_number
        return self


def exit_percentages(settings, strategy):
    """Support injected lightweight settings while sharing production defaults."""
    names = (f'{strategy}_profit_percent', f'{strategy}_stop_loss_percent')
    return tuple(getattr(settings, name, StrategyExitSettings.model_fields[name].default) for name in names)


def options_margin_account_id(module, settings):
    account_number = getattr(settings, 'options_margin_account_number', '').strip()
    if not account_number:
        raise ValueError('Set OPTIONS_MARGIN_ACCOUNT_NUMBER (WEBULL_LIVE_OPTIONS_MARGIN_ACCOUNT_NUMBER in live mode) before submitting bearish or iron-condor orders')
    return module.get_account_id(account_number=account_number)


def bullish_stock_account_id(module, settings):
    account_number = getattr(settings, 'bullish_stock_account_number', '').strip()
    if not account_number:
        raise ValueError('Set BULLISH_STOCK_ACCOUNT_NUMBER (WEBULL_LIVE_BULLISH_STOCK_ACCOUNT_NUMBER in live mode) before submitting bullish stock orders')
    return module.get_account_id(account_number=account_number)
