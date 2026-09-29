"""Bullish-flow fixed-dollar stock runner polling every five minutes; DRY_RUN=true by default."""

from __future__ import annotations


import argparse
import json
import logging
import math
import re
import sys
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.common.paths import ENV_FILE

from pydantic import Field
from pydantic_settings import SettingsConfigDict
from app.config.strategy import StrategyExitSettings

from app.bullish.ledger import BullishLedger
from app.exits.next_day import ExitCalendar
from app.broker.quotes import QuoteError, current_stock_quote
from app.broker.errors import describe_webull_error
from app.execution.submitter import _load_webull_stock_module

logger = logging.getLogger(__name__)


class MarketClosed(Exception):
    """The trading session ended before submission."""


class BullishSettings(StrategyExitSettings):
    account_number: str = Field(default='', alias='TOP_BULLISH_ACCOUNT_NUMBER')
    dry_run: bool = Field(default=True, alias='DRY_RUN')
    database_path: str = Field(default='bot.sqlite3', alias='DATABASE_PATH')
    max_notional_usd: float = Field(default=250, gt=0, allow_inf_nan=False, alias='MAX_NOTIONAL_USD')
    model_config = SettingsConfigDict(
        env_file=ENV_FILE, extra='ignore',
    )


def load_feed():
    from app.bullish.feed import TopBullish
    return TopBullish()


class MainTopBullish:
    def __init__(self, *, settings=None, feed=None, stock_loader=None, quote_provider=None,
                 market_open=None):
        self.settings = settings or BullishSettings()
        self.ledger = BullishLedger(self.settings.database_path)
        self.quote_provider = quote_provider or current_stock_quote
        self.feed = feed
        self.stock_loader = stock_loader or _load_webull_stock_module
        if market_open is None:
            calendar = ExitCalendar(exit_time='09:35', timezone='America/New_York')
            market_open = lambda: calendar.is_open(datetime.now(UTC))
        self.market_open = market_open

    def run_forever(self, *, limit=10):
        """Run immediately, then every 300 seconds, skipping missed ticks."""
        next_run = time.monotonic()
        cycle = 0
        logger.info('Bullish scheduler started: interval=300s; press Ctrl+C to stop')
        try:
            while True:
                cycle += 1
                logger.info('Starting bullish-flow scan #%s', cycle)
                try:
                    results = self.run(limit=limit)
                    print(json.dumps(results, indent=2), flush=True)
                    counts = dict(Counter(row.get('status', 'unknown') for row in results))
                    logger.info('Bullish-flow scan #%s completed: %s', cycle, counts)
                except Exception as exc:
                    logger.error('Bullish-flow scan failed: %s; retrying next cycle',
                                 describe_webull_error(exc))
                next_run += 300
                now = time.monotonic()
                if next_run < now:
                    next_run += (int((now - next_run) // 300) + 1) * 300
                scheduled = datetime.now().astimezone() + timedelta(seconds=next_run - now)
                logger.info('Next bullish-flow scan at %s (in %.0f seconds)',
                            scheduled.isoformat(timespec='seconds'), next_run - now)
                time.sleep(next_run - now)
        except KeyboardInterrupt:
            logger.info('Bullish-flow scheduler stopped')

    def run(self, *, limit=10):
        """Process feed symbols once using fresh Webull quotes as limit prices."""
        if not self.market_open():
            logger.info('Skipping bullish scan; market closed (America/New_York)')
            return [{'status': 'skipped', 'reason': 'outside_market_hours'}]
        payload = (self.feed or load_feed()).fetch(limit=limit)
        if not isinstance(payload, list):
            raise ValueError('Expected a list of bullish flow entries')
        results = []
        for item in payload:
            try:
                results.append(self.process(item))
            except (ValueError, TypeError, KeyError) as exc:
                results.append({'status': 'skipped', 'reason': str(exc)})
        return results

    def process(self, item):
        if not self.market_open():
            return {'status': 'skipped', 'reason': 'outside_market_hours'}
        if not isinstance(item, dict):
            raise ValueError('Bullish flow entry must be an object')
        symbol = str(item.get('symbol') or '').strip().upper()
        if not re.fullmatch(r'[A-Z][A-Z0-9.]{0,9}', symbol):
            raise ValueError('Invalid bullish symbol')
        # The aggregate feed has no trade ID; symbol is the durable identity.
        trade_id = str(item.get('trade_id') or item.get('id') or f'bullish:{symbol}')
        if self.ledger.contains(trade_id, symbol):
            return {'symbol': symbol, 'status': 'skipped', 'reason': 'Trade or symbol already present'}
        premium = float(item['total_premium'])
        count = item['trade_count']
        if not math.isfinite(premium) or premium < 0:
            raise ValueError(f'{symbol}: invalid total_premium')
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError(f'{symbol}: invalid trade_count')
        try:
            quote = self.quote_provider(symbol)
        except QuoteError as exc:
            logger.warning('Webull quote for %s: %s', symbol, exc)
            return {'symbol': symbol, 'status': 'skipped', 'reason': f'Quote unavailable: {exc}'}
        except Exception as exc:
            detail = describe_webull_error(exc)
            logger.error('Webull quote for %s failed: %s', symbol, detail)
            return {'symbol': symbol, 'status': 'skipped', 'reason': f'Quote unavailable: {detail}'}
        from app.bullish.stock_bracket import validate_stock_buy
        try:
            validate_stock_buy(float(quote['price']), self.settings.max_notional_usd)
        except ValueError as exc:
            return {'symbol': symbol, 'status': 'skipped', 'reason': str(exc)}
        order_request = dict(symbol=symbol)
        normalized = {**item, 'symbol': symbol, 'total_premium': premium, 'trade_count': count, 'entry_quote': quote}
        if not self.market_open():
            return {'symbol': symbol, 'status': 'skipped', 'reason': 'outside_market_hours'}
        if not self.settings.dry_run:
            if not self.settings.account_number.strip():
                raise ValueError('Set TOP_BULLISH_ACCOUNT_NUMBER before submitting orders')
            stock = self.stock_loader()
            account_id = stock.get_account_id(account_number=self.settings.account_number.strip())
        if not self.ledger.claim(trade_id, normalized, {**order_request, 'notional_usd': 100.0, 'order_type': 'MARKET', 'time_in_force': 'DAY'}):
            return {'symbol': symbol, 'status': 'skipped', 'reason': 'Trade or symbol already present'}
        if self.settings.dry_run:
            self.ledger.update(trade_id, 'dry_run')
            return {'symbol': symbol, 'status': 'dry_run', 'order': {**order_request, 'notional_usd': 100.0, 'order_type': 'MARKET', 'time_in_force': 'DAY'}}
        attempted = False

        def before_submit(tracking):
            nonlocal attempted
            if not self.market_open():
                raise MarketClosed()
            self.ledger.update(trade_id, 'submitting', tracking=tracking)
            attempted = True

        try:
            result = stock.buy_stock(
                account_id=account_id, **order_request,
                before_submit=before_submit,
            )
            self.ledger.update(trade_id, 'submitted', order=result)
            return {'symbol': symbol, 'status': 'submitted'}
        except MarketClosed:
            self.ledger.update(trade_id, 'skipped', error='outside_market_hours')
            return {'symbol': symbol, 'status': 'skipped', 'reason': 'outside_market_hours'}
        except Exception as exc:
            # A timeout after submission may still mean the broker accepted it.
            status = 'submission_unknown' if attempted else 'failed'
            detail = describe_webull_error(exc)
            logger.error('Webull order for %s (%s): %s', symbol, status, detail)
            self.ledger.update(trade_id, status, error=type(exc).__name__)
            return {'symbol': symbol, 'status': status, 'reason': detail}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--limit', type=int, default=10)
    parser.add_argument('--once', action='store_true', help='Run one scan and exit')
    args = parser.parse_args()
    if args.limit < 1:
        parser.error('--limit must be positive')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    runner = MainTopBullish()
    if args.once:
        print(json.dumps(runner.run(limit=args.limit), indent=2), flush=True)
    else:
        runner.run_forever(limit=args.limit)
