from decimal import Decimal
from typing import TYPE_CHECKING, Dict, Optional

from hummingbot.connector.utils import split_hb_trading_pair
from hummingbot.core.rate_oracle.sources.rate_source_base import RateSourceBase
from hummingbot.core.utils import async_ttl_cache
from hummingbot.core.utils.async_utils import safe_gather

if TYPE_CHECKING:
    from hummingbot.connector.derivative.backpack_perpetual.backpack_perpetual_derivative import BackpackPerpetualDerivative


class BackpackRateSource(RateSourceBase):
    def __init__(self):
        super().__init__()
        self._backpack_exchange: Optional[BackpackPerpetualDerivative] = None  # delayed because of circular reference

    @property
    def name(self) -> str:
        return "backpack"

    @async_ttl_cache(ttl=30, maxsize=1)
    async def get_prices(self, quote_token: Optional[str] = None) -> Dict[str, Decimal]:
        self._ensure_exchanges()
        results = {}
        tasks = [
            self._get_backpack_prices(exchange=self._backpack_exchange),
        ]
        task_results = await safe_gather(*tasks, return_exceptions=True)
        for task_result in task_results:
            if isinstance(task_result, Exception):
                self.logger().error(
                    msg="Unexpected error while retrieving rates from Binance. Check the log file for more info.",
                    exc_info=task_result,
                )
                break
            else:
                results.update(task_result)
        return results

    def _ensure_exchanges(self):
        if self._backpack_exchange is None:
            self._backpack_exchange = self._build_connector_without_private_keys(domain="com")

    @staticmethod
    async def _get_backpack_prices(exchange: 'BackpackPerpetualDerivative', quote_token: str = None) -> Dict[str, Decimal]:
        """
        Fetches binance prices

        :param exchange: The exchange instance from which to query prices.
        :param quote_token: A quote symbol, if specified only pairs with the quote symbol are included for prices
        :return: A dictionary of trading pairs and prices
        """
        pairs_prices = await exchange.get_all_pairs_prices()
        """
          {
            "firstPrice": "150.49",
            "high": "153.81",
            "lastPrice": "149.25",
            "low": "144.54",
            "priceChange": "-1.24",
            "priceChangePercent": "-0.00824",
            "quoteVolume": "19738.1981",
            "symbol": "AAVE_USDC",
            "trades": "277",
            "volume": "133.117"
          },
      """
        results = {}
        for pair_price in pairs_prices:
            try:
                trading_pair = await exchange.trading_pair_associated_to_exchange_symbol(symbol=pair_price["symbol"])
            except KeyError:
                continue  # skip pairs that we don't track
            if quote_token is not None:
                base, quote = split_hb_trading_pair(trading_pair=trading_pair)
                if quote != quote_token:
                    continue
            bid_price = pair_price.get("high")
            ask_price = pair_price.get("askPrice")
            if bid_price is not None and ask_price is not None and 0 < Decimal(bid_price) <= Decimal(ask_price):
                results[trading_pair] = (Decimal(bid_price) + Decimal(ask_price)) / Decimal("2")

        return results

    @staticmethod
    def _build_connector_without_private_keys(domain: str) -> 'BackpackPerpetualDerivative':
        from hummingbot.client.hummingbot_application import HummingbotApplication

        app = HummingbotApplication.main_application()
        client_config_map = app.client_config_map

        return BackpackPerpetualDerivative(
            client_config_map=client_config_map,
            backpack_api_key="",
            backpack_secret_key="",
            trading_pairs=[],
            trading_required=False,
            domain=domain,
        )
