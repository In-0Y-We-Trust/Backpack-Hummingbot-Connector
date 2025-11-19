import asyncio
import functools
import json
import re
from decimal import Decimal

from six import assertCountEqual

from hummingbot.connector.derivative.position import Position
from test.isolated_asyncio_wrapper_test_case import IsolatedAsyncioWrapperTestCase
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
from aioresponses.core import aioresponses
from bidict import bidict

import hummingbot.connector.derivative.backpack_perpetual.backpack_perpetual_constants as CONSTANTS
import hummingbot.connector.derivative.backpack_perpetual.backpack_perpetual_web_utils as web_utils
from hummingbot.client.config.client_config_map import ClientConfigMap
from hummingbot.client.config.config_helpers import ClientConfigAdapter
from hummingbot.connector.derivative.backpack_perpetual.backpack_perpetual_api_order_book_data_source import (
    BackpackPerpetualAPIOrderBookDataSource,
)
from hummingbot.connector.derivative.backpack_perpetual.backpack_perpetual_derivative import BackpackPerpetualDerivative
from hummingbot.connector.test_support.network_mocking_assistant import NetworkMockingAssistant
from hummingbot.connector.trading_rule import TradingRule
from hummingbot.connector.utils import get_new_client_order_id, get_new_numeric_client_order_id
from hummingbot.core.data_type.common import OrderType, PositionAction, PositionMode, TradeType, PositionSide
from hummingbot.core.data_type.in_flight_order import InFlightOrder, OrderState
from hummingbot.core.data_type.limit_order import LimitOrder
from hummingbot.core.data_type.trade_fee import TokenAmount
from hummingbot.core.event.event_logger import EventLogger
from hummingbot.core.event.events import MarketEvent, OrderFilledEvent


class BackpackPerpetualDerivativeUnitTest(IsolatedAsyncioWrapperTestCase):
    # the level is required to receive logs from the data source logger
    level = 0

    start_timestamp: float = pd.Timestamp("2021-01-01", tz="UTC").timestamp()

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.base_asset = "SOL"
        cls.quote_asset = "HBOT"
        cls.trading_pair = f"{cls.base_asset}-{cls.quote_asset}"
        cls.symbol = f"{cls.base_asset}_{cls.quote_asset}_PERP"
        cls.domain = CONSTANTS.DEFAULT_DOMAIN

    def setUp(self) -> None:
        super().setUp()
        self.log_records = []

        self.ws_sent_messages = []
        self.ws_incoming_messages = asyncio.Queue()
        self.resume_test_event = asyncio.Event()
        self.client_config_map = ClientConfigAdapter(ClientConfigMap())

        self.exchange = BackpackPerpetualDerivative(
            client_config_map=self.client_config_map,
            backpack_api_key="testAPIKey",
            backpack_secret_key="8JBN5Io91Do1YN9UatBqhfDP1A4MAkZ3ZS2eM78WBA4=",
            trading_pairs=[self.trading_pair],
            domain=self.domain,
        )

        if hasattr(self.exchange, "_time_synchronizer"):
            self.exchange._time_synchronizer.add_time_offset_ms_sample(0)
            self.exchange._time_synchronizer.logger().setLevel(1)
            self.exchange._time_synchronizer.logger().addHandler(self)

        BackpackPerpetualAPIOrderBookDataSource._trading_pair_symbol_map = {
            self.domain: bidict({self.symbol: self.trading_pair})
        }

        self.exchange._set_current_timestamp(1640780000)
        self.exchange.logger().setLevel(1)
        self.exchange.logger().addHandler(self)
        self.exchange._order_tracker.logger().setLevel(1)
        self.exchange._order_tracker.logger().addHandler(self)
        self.mocking_assistant = NetworkMockingAssistant(self.local_event_loop)
        self.test_task: Optional[asyncio.Task] = None
        self.resume_test_event = asyncio.Event()
        self._initialize_event_loggers()

    @property
    def all_symbols_url(self):
        url = web_utils.public_rest_url(path_url=CONSTANTS.MARKET_PATH_URL)
        return url

    @property
    def latest_prices_url(self):
        url = web_utils.public_rest_url(
            path_url=CONSTANTS.TICKER_PATH_URL
        )
        url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?") + ".*")
        return url

    @property
    def network_status_url(self):
        url = web_utils.public_rest_url(path_url=CONSTANTS.STATUS_PATH_URL)
        return url

    @property
    def trading_rules_url(self):
        url = web_utils.public_rest_url(path_url=CONSTANTS.MARKET_PATH_URL)
        return url

    @property
    def balance_url(self):
        url = web_utils.private_rest_url(path_url=CONSTANTS.ACCOUNTS_PATH_URL)
        return url

    @property
    def funding_info_url(self):
        url = web_utils.public_rest_url(
            path_url=CONSTANTS.TICKER_PATH_URL
        )
        url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?") + ".*")
        return url

    @property
    def funding_payment_url(self):
        url = web_utils.private_rest_url(
            path_url=CONSTANTS.GET_INCOME_HISTORY_URL
        )
        url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?") + ".*")
        return url

    def tearDown(self) -> None:
        self.test_task and self.test_task.cancel()
        BackpackPerpetualAPIOrderBookDataSource._trading_pair_symbol_map = {}
        super().tearDown()

    def _initialize_event_loggers(self):
        self.buy_order_completed_logger = EventLogger()
        self.sell_order_completed_logger = EventLogger()
        self.order_cancelled_logger = EventLogger()
        self.order_filled_logger = EventLogger()
        self.funding_payment_completed_logger = EventLogger()

        events_and_loggers = [
            (MarketEvent.BuyOrderCompleted, self.buy_order_completed_logger),
            (MarketEvent.SellOrderCompleted, self.sell_order_completed_logger),
            (MarketEvent.OrderCancelled, self.order_cancelled_logger),
            (MarketEvent.OrderFilled, self.order_filled_logger),
            (MarketEvent.FundingPaymentCompleted, self.funding_payment_completed_logger)]

        for event, logger in events_and_loggers:
            self.exchange.add_listener(event, logger)

    def handle(self, record):
        self.log_records.append(record)

    def _is_logged(self, log_level: str, message: str) -> bool:
        return any(record.levelname == log_level and record.getMessage() == message for record in self.log_records)

    def _create_exception_and_unlock_test_with_event(self, exception):
        self.resume_test_event.set()
        raise exception

    def _return_calculation_and_set_done_event(self, calculation: Callable, *args, **kwargs):
        if self.resume_test_event.is_set():
            raise asyncio.CancelledError
        self.resume_test_event.set()
        return calculation(*args, **kwargs)

    def _get_position_risk_api_endpoint_single_position_list(self) -> List[Dict[str, Any]]:

        positions = [
            {
                "breakEvenPrice": "116.9338590909090909090909091",
                "cumulativeFundingPayment": "0.017929",
                "cumulativeInterest": "0",
                "entryPrice": "116.13545454545454545454545455",
                "estLiquidationPrice": "0",
                "imf": "0.02",
                "imfFunction": {
                    "base": "0.02",
                    "factor": "0.00015",
                    "type": "sqrt"
                },
                "markPrice": "115.34732014",
                "mmf": "0.0125",
                "mmfFunction": {
                    "base": "0.0125",
                    "factor": "0.000092",
                    "type": "sqrt"
                },
                "netCost": "127.749",
                "netExposureNotional": "126.882052154",
                "netExposureQuantity": "1.1",
                "netQuantity": "1.1",
                "pnlRealized": "-0.878245",
                "pnlUnrealized": "0.011297",
                "positionId": "1530437281",
                "symbol": self.symbol,
                "userId": 1728445
            }
        ]
        return positions

    def _get_wrong_symbol_position_risk_api_endpoint_single_position_list(self) -> List[Dict[str, Any]]:
        positions = [
            {
                "symbol": f"{self.symbol}_230331",
                "positionAmt": "1",
                "entryPrice": "10",
                "markPrice": "11",
                "unRealizedProfit": "1",
                "liquidationPrice": "100",
                "leverage": "1",
                "maxNotionalValue": "9",
                "marginType": "cross",
                "isolatedMargin": "0",
                "isAutoAddMargin": "false",
                "positionSide": "BOTH",
                "notional": "11",
                "isolatedWallet": "0",
                "updateTime": int(self.start_timestamp),
            }
        ]
        return positions

    def _get_account_update_ws_event_single_position_dict(self) -> Dict[str, Any]:
        account_update = {
            "e": "ACCOUNT_UPDATE",
            "E": 1564745798939,
            "T": 1564745798938,
            "a": {
                "m": "POSITION",
                "B": [
                    {"a": "USDT", "wb": "122624.12345678", "cw": "100.12345678", "bc": "50.12345678"},
                ],
                "P": [
                    {
                        "s": self.symbol,
                        "pa": "1",
                        "ep": "10",
                        "cr": "200",
                        "up": "1",
                        "mt": "cross",
                        "iw": "0.00000000",
                        "ps": "BOTH",
                    },
                ],
            },
        }
        return account_update

    def _get_wrong_symbol_account_update_ws_event_single_position_dict(self) -> Dict[str, Any]:
        account_update = {
            "e": "ACCOUNT_UPDATE",
            "E": 1564745798939,
            "T": 1564745798938,
            "a": {
                "m": "POSITION",
                "B": [
                    {"a": "USDT", "wb": "122624.12345678", "cw": "100.12345678", "bc": "50.12345678"},
                ],
                "P": [
                    {
                        "s": f"{self.symbol}_230331",
                        "pa": "1",
                        "ep": "10",
                        "cr": "200",
                        "up": "1",
                        "mt": "cross",
                        "iw": "0.00000000",
                        "ps": "BOTH",
                    },
                ],
            },
        }
        return account_update

    def _get_income_history_dict(self) -> List:
        income_history = [{
            "income": 1,
            "symbol": self.symbol,
            "time": self.start_timestamp,
        }]
        return income_history

    def _get_funding_info_dict(self) -> Dict[str, Any]:
        funding_info = {
            "indexPrice": 1000,
            "markPrice": 1001,
            "nextFundingTime": self.start_timestamp + 8 * 60 * 60,
            "lastFundingRate": 1010
        }
        return funding_info

    def _get_trading_pair_symbol_map(self) -> Dict[str, str]:
        trading_pair_symbol_map = {self.symbol: f"{self.base_asset}-{self.quote_asset}"}
        return trading_pair_symbol_map

    def _get_exchange_info_mock_response(
            self,
            margin_asset: str = "HBOT",
            min_order_size: float = 1,
            min_price_increment: float = 2,
            min_base_amount_increment: float = 3,
            min_notional_size: float = 4,
    ) -> List[ Any]:
        mocked_exchange_info = [
            {
                "baseSymbol": "SOL",
                "createdAt": "2025-01-21T06:34:54.691858",
                "filters": {
                    "price": {
                        "maxImpactMultiplier": "1.1",
                        "maxMultiplier": "1.1",
                        "maxPrice": "1000",
                        "meanMarkPriceBand": {
                            "maxMultiplier": "1.1",
                            "minMultiplier": "0.9"
                        },
                        "meanPremiumBand": {
                            "tolerancePct": "0.01"
                        },
                        "minImpactMultiplier": "0.9",
                        "minMultiplier": "0.9",
                        "minPrice": str(min_price_increment),
                        "tickSize": "0.01"
                    },
                    "quantity": {
                        "maxQuantity": None,
                        "minQuantity": str(min_order_size),
                        "stepSize": str(min_base_amount_increment)
                    }
                },
                "fundingInterval": 28800000,
                "imfFunction": {
                    "base": "0.02",
                    "factor": "0.00015",
                    "type": "sqrt"
                },
                "marketType": "PERP",
                "mmfFunction": {
                    "base": "0.0125",
                    "factor": "0.000092",
                    "type": "sqrt"
                },
                "openInterestLimit": "600000",
                "orderBookState": "Open",
                "quoteSymbol": margin_asset,
                "symbol": "SOL_"+ margin_asset + "_PERP"
            },
        ]

        return mocked_exchange_info

    def _get_exchange_info_error_mock_response(
            self,
            margin_asset: str = "HBOT",
            min_order_size: float = 1,
            min_price_increment: float = 2,
            min_base_amount_increment: float = 3,
            min_notional_size: float = 4,
    ) -> Dict[str, Any]:
        mocked_exchange_info = {  # irrelevant fields removed
            "symbols": [
                {
                    "symbol": self.symbol,
                    "pair": self.symbol,
                    "contractType": "PERPETUAL",
                    "baseAsset": self.base_asset,
                    "quoteAsset": self.quote_asset,
                    "marginAsset": margin_asset,
                    "status": "TRADING",
                }
            ],
        }

        return mocked_exchange_info

    @aioresponses()
    async def test_existing_account_position_detected_on_positions_update(self, req_mock):
        self._simulate_trading_rules_initialized()

        url = web_utils.private_rest_url(
            CONSTANTS.POSITION_INFORMATION_URL, domain=self.domain
        )
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))

        positions = self._get_position_risk_api_endpoint_single_position_list()
        req_mock.get(regex_url, body=json.dumps(positions))

        await self.exchange._update_positions()

        self.assertEqual(len(self.exchange.account_positions), 1)
        pos = list(self.exchange.account_positions.values())[0]
        self.assertEqual(pos.trading_pair.replace("-", ""), self.symbol)

    @aioresponses()
    async def test_wrong_symbol_position_detected_on_positions_update(self, req_mock):
        self._simulate_trading_rules_initialized()

        url = web_utils.private_rest_url(
            CONSTANTS.POSITION_INFORMATION_URL, domain=self.domain
        )
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))

        positions = self._get_wrong_symbol_position_risk_api_endpoint_single_position_list()
        req_mock.get(regex_url, body=json.dumps(positions))

        await self.exchange._update_positions()

        self.assertEqual(len(self.exchange.account_positions), 0)

    @aioresponses()
    async def test_account_position_updated_on_positions_update(self, req_mock):
        self._simulate_trading_rules_initialized()
        url = web_utils.private_rest_url(
            CONSTANTS.POSITIONS_PATH_URL, domain=self.domain
        )
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))

        positions = self._get_position_risk_api_endpoint_single_position_list()
        req_mock.get(regex_url, body=json.dumps(positions))

        await self.exchange._update_positions()

        self.assertEqual(len(self.exchange.account_positions), 1)
        pos = list(self.exchange.account_positions.values())[0]
        self.assertEqual(pos.amount, Decimal('1.1'))

        positions[0]["netQuantity"] = "2"
        req_mock.get(regex_url, body=json.dumps(positions))
        await self.exchange._update_positions()

        pos = list(self.exchange.account_positions.values())[0]
        self.assertEqual(pos.amount, Decimal('2'))

    @aioresponses()
    async def test_new_account_position_detected_on_positions_update(self, req_mock):
        self._simulate_trading_rules_initialized()
        url = web_utils.private_rest_url(
            CONSTANTS.POSITIONS_PATH_URL, domain=self.domain
        )
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))

        req_mock.get(regex_url, body=json.dumps([]))

        await self.exchange._update_positions()

        self.assertEqual(len(self.exchange.account_positions), 0)

        positions = self._get_position_risk_api_endpoint_single_position_list()
        req_mock.get(regex_url, body=json.dumps(positions))
        await self.exchange._update_positions()

        self.assertEqual(len(self.exchange.account_positions), 1)

    @aioresponses()
    async def test_closed_account_position_removed_on_positions_update(self, req_mock):
        self._simulate_trading_rules_initialized()
        url = web_utils.private_rest_url(
            CONSTANTS.POSITIONS_PATH_URL, domain=self.domain
        )
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))

        positions = self._get_position_risk_api_endpoint_single_position_list()
        req_mock.get(regex_url, body=json.dumps(positions))

        await self.exchange._update_positions()

        self.assertEqual(len(self.exchange.account_positions), 1)

        positions[0]["netQuantity"] = "0"
        req_mock.get(regex_url, body=json.dumps(positions))
        await self.exchange._update_positions()

        self.assertEqual(len(self.exchange.account_positions), 0)

    async def test_supported_position_modes(self):
        linear_connector = self.exchange
        expected_result = [PositionMode.ONEWAY, PositionMode.HEDGE]
        self.assertEqual(expected_result, linear_connector.supported_position_modes())

    async def test_format_trading_rules(self):
        margin_asset = self.quote_asset
        min_order_size = "0.01"
        min_price_increment = "0.01"
        min_base_amount_increment = "0.01"
        min_notional_size = 4
        mocked_response = self._get_exchange_info_mock_response(
            margin_asset, min_order_size, min_price_increment, min_base_amount_increment, min_notional_size
        )
        self._simulate_trading_rules_initialized()
        trading_rules = await self.exchange._format_trading_rules(mocked_response)

        self.assertEqual(1, len(trading_rules))

        trading_rule = trading_rules[0]

        self.assertEqual(Decimal(min_order_size), trading_rule.min_order_size)
        self.assertEqual(Decimal(min_price_increment), trading_rule.min_price_increment)
        self.assertEqual(Decimal(min_base_amount_increment), trading_rule.min_base_amount_increment)
        self.assertEqual(margin_asset, trading_rule.buy_order_collateral_token)
        self.assertEqual(margin_asset, trading_rule.sell_order_collateral_token)

    async def test_format_trading_rules_exception(self):
        margin_asset = self.quote_asset
        min_order_size = 1
        min_price_increment = 2
        min_base_amount_increment = 3
        min_notional_size = 4
        mocked_response = self._get_exchange_info_error_mock_response(
            margin_asset, min_order_size, min_price_increment, min_base_amount_increment, min_notional_size
        )
        self._simulate_trading_rules_initialized()

        await self.exchange._format_trading_rules(mocked_response)
        self.assertTrue(self._is_logged(
            "ERROR",
            f"Error parsing the trading pair rule {mocked_response['symbols'][0]}. Error: 'filters'. Skipping..."
        ))

    async def test_get_collateral_token(self):
        margin_asset = self.quote_asset
        self._simulate_trading_rules_initialized()

        self.assertEqual(margin_asset, self.exchange.get_buy_collateral_token(self.trading_pair))
        self.assertEqual(margin_asset, self.exchange.get_sell_collateral_token(self.trading_pair))

    async def test_buy_order_fill_event_takes_fee_from_update_event(self):
        self.exchange.start_tracking_order(
            order_id="11113333",
            exchange_order_id="8886774",
            trading_pair=self.trading_pair,
            trade_type=TradeType.BUY,
            price=Decimal("10000"),
            amount=Decimal("1"),
            order_type=OrderType.LIMIT,
            leverage=1,
            position_action=PositionAction.OPEN,
        )

        order = self.exchange.in_flight_orders.get("11113333")

        partial_fill = {
            "e": "ORDER_TRADE_UPDATE",
            "E": 1568879465651,
            "T": 1568879465650,
            "o": {
                "s": self.trading_pair,
                "c": order.client_order_id,
                "S": "BUY",
                "o": "TRAILING_STOP_MARKET",
                "f": "GTC",
                "q": "1",
                "p": "10000",
                "ap": "0",
                "sp": "7103.04",
                "x": "TRADE",
                "X": "PARTIALLY_FILLED",
                "i": 8886774,
                "l": "0.1",
                "z": "0.1",
                "L": "10000",
                "N": "HBOT",
                "n": "20",
                "T": 1568879465651,
                "t": 1,
                "b": "0",
                "a": "9.91",
                "m": False,
                "R": False,
                "wt": "CONTRACT_PRICE",
                "ot": "TRAILING_STOP_MARKET",
                "ps": "LONG",
                "cp": False,
                "AP": "7476.89",
                "cr": "5.0",
                "rp": "0"
            }

        }

        mock_user_stream = AsyncMock()
        mock_user_stream.get.side_effect = functools.partial(self._return_calculation_and_set_done_event,
                                                             lambda: partial_fill)

        self.exchange._user_stream_tracker._user_stream = mock_user_stream

        self.test_task = self.local_event_loop.create_task(self.exchange._user_stream_event_listener())
        await self.resume_test_event.wait()

        self.assertEqual(1, len(self.order_filled_logger.event_log))
        fill_event: OrderFilledEvent = self.order_filled_logger.event_log[0]
        self.assertEqual(Decimal("0"), fill_event.trade_fee.percent)
        self.assertEqual(
            [TokenAmount(partial_fill["o"]["N"], Decimal(partial_fill["o"]["n"]))], fill_event.trade_fee.flat_fees
        )

        complete_fill = {
            "e": "ORDER_TRADE_UPDATE",
            "E": 1568879465651,
            "T": 1568879465650,
            "o": {
                "s": self.trading_pair,
                "c": order.client_order_id,
                "S": "BUY",
                "o": "TRAILING_STOP_MARKET",
                "f": "GTC",
                "q": "1",
                "p": "10000",
                "ap": "0",
                "sp": "7103.04",
                "x": "TRADE",
                "X": "FILLED",
                "i": 8886774,
                "l": "0.9",
                "z": "1",
                "L": "10000",
                "N": "HBOT",
                "n": "30",
                "T": 1568879465651,
                "t": 2,
                "b": "0",
                "a": "9.91",
                "m": False,
                "R": False,
                "wt": "CONTRACT_PRICE",
                "ot": "TRAILING_STOP_MARKET",
                "ps": "LONG",
                "cp": False,
                "AP": "7476.89",
                "cr": "5.0",
                "rp": "0"
            }

        }

        self.resume_test_event = asyncio.Event()
        mock_user_stream.get.side_effect = functools.partial(self._return_calculation_and_set_done_event,
                                                             lambda: complete_fill)

        self.test_task = self.local_event_loop.create_task(self.exchange._user_stream_event_listener())
        await self.resume_test_event.wait()

        self.assertEqual(2, len(self.order_filled_logger.event_log))
        fill_event: OrderFilledEvent = self.order_filled_logger.event_log[1]
        self.assertEqual(Decimal("0"), fill_event.trade_fee.percent)
        self.assertEqual([TokenAmount(complete_fill["o"]["N"], Decimal(complete_fill["o"]["n"]))],
                         fill_event.trade_fee.flat_fees)

    async def test_sell_order_fill_event_takes_fee_from_update_event(self):
        self.exchange.start_tracking_order(
            order_id="11113333",
            exchange_order_id="8886774",
            trading_pair=self.trading_pair,
            trade_type=TradeType.SELL,
            price=Decimal("10000"),
            amount=Decimal("1"),
            order_type=OrderType.LIMIT,
            leverage=1,
            position_action=PositionAction.OPEN,
        )

        order = self.exchange.in_flight_orders.get("11113333")

        partial_fill = {
            "e": "ORDER_TRADE_UPDATE",
            "E": 1568879465651,
            "T": 1568879465650,
            "o": {
                "s": self.trading_pair,
                "c": order.client_order_id,
                "S": "SELL",
                "o": "TRAILING_STOP_MARKET",
                "f": "GTC",
                "q": "1",
                "p": "10000",
                "ap": "0",
                "sp": "7103.04",
                "x": "TRADE",
                "X": "PARTIALLY_FILLED",
                "i": 8886774,
                "l": "0.1",
                "z": "0.1",
                "L": "10000",
                "N": self.quote_asset,
                "n": "20",
                "T": 1568879465651,
                "t": 1,
                "b": "0",
                "a": "9.91",
                "m": False,
                "R": False,
                "wt": "CONTRACT_PRICE",
                "ot": "TRAILING_STOP_MARKET",
                "ps": "LONG",
                "cp": False,
                "AP": "7476.89",
                "cr": "5.0",
                "rp": "0"
            }
        }

        mock_user_stream = AsyncMock()
        mock_user_stream.get.side_effect = functools.partial(self._return_calculation_and_set_done_event,
                                                             lambda: partial_fill)

        self.exchange._user_stream_tracker._user_stream = mock_user_stream

        self.test_task = self.local_event_loop.create_task(self.exchange._user_stream_event_listener())
        await self.resume_test_event.wait()

        self.assertEqual(1, len(self.order_filled_logger.event_log))
        fill_event: OrderFilledEvent = self.order_filled_logger.event_log[0]
        self.assertEqual(Decimal("0"), fill_event.trade_fee.percent)
        self.assertEqual(
            [TokenAmount(partial_fill["o"]["N"], Decimal(partial_fill["o"]["n"]))], fill_event.trade_fee.flat_fees
        )

        complete_fill = {
            "e": "ORDER_TRADE_UPDATE",
            "E": 1568879465651,
            "T": 1568879465650,
            "o": {
                "s": self.trading_pair,
                "c": order.client_order_id,
                "S": "SELL",
                "o": "TRAILING_STOP_MARKET",
                "f": "GTC",
                "q": "1",
                "p": "10000",
                "ap": "0",
                "sp": "7103.04",
                "x": "TRADE",
                "X": "FILLED",
                "i": 8886774,
                "l": "0.9",
                "z": "1",
                "L": "10000",
                "N": self.quote_asset,
                "n": "30",
                "T": 1568879465651,
                "t": 2,
                "b": "0",
                "a": "9.91",
                "m": False,
                "R": False,
                "wt": "CONTRACT_PRICE",
                "ot": "TRAILING_STOP_MARKET",
                "ps": "LONG",
                "cp": False,
                "AP": "7476.89",
                "cr": "5.0",
                "rp": "0"
            }

        }

        self.resume_test_event = asyncio.Event()
        mock_user_stream.get.side_effect = functools.partial(self._return_calculation_and_set_done_event,
                                                             lambda: complete_fill)

        self.test_task = self.local_event_loop.create_task(self.exchange._user_stream_event_listener())
        await self.resume_test_event.wait()

        self.assertEqual(2, len(self.order_filled_logger.event_log))
        fill_event: OrderFilledEvent = self.order_filled_logger.event_log[1]
        self.assertEqual(Decimal("0"), fill_event.trade_fee.percent)
        self.assertEqual([TokenAmount(complete_fill["o"]["N"], Decimal(complete_fill["o"]["n"]))],
                         fill_event.trade_fee.flat_fees)

    async def test_order_fill_event_ignored_for_repeated_trade_id(self):
        self.exchange.start_tracking_order(
            order_id="11113333",
            exchange_order_id="8886774",
            trading_pair=self.trading_pair,
            trade_type=TradeType.BUY,
            price=Decimal("10000"),
            amount=Decimal("1"),
            order_type=OrderType.LIMIT,
            leverage=1,
            position_action=PositionAction.OPEN,
        )

        order = self.exchange.in_flight_orders.get("11113333")

        partial_fill = {
            "e": "ORDER_TRADE_UPDATE",
            "E": 1568879465651,
            "T": 1568879465650,
            "o": {
                "s": self.trading_pair,
                "c": order.client_order_id,
                "S": "BUY",
                "o": "TRAILING_STOP_MARKET",
                "f": "GTC",
                "q": "1",
                "p": "10000",
                "ap": "0",
                "sp": "7103.04",
                "x": "TRADE",
                "X": "PARTIALLY_FILLED",
                "i": 8886774,
                "l": "0.1",
                "z": "0.1",
                "L": "10000",
                "N": self.quote_asset,
                "n": "20",
                "T": 1568879465651,
                "t": 1,
                "b": "0",
                "a": "9.91",
                "m": False,
                "R": False,
                "wt": "CONTRACT_PRICE",
                "ot": "TRAILING_STOP_MARKET",
                "ps": "LONG",
                "cp": False,
                "AP": "7476.89",
                "cr": "5.0",
                "rp": "0"
            }
        }

        mock_user_stream = AsyncMock()
        mock_user_stream.get.side_effect = functools.partial(self._return_calculation_and_set_done_event,
                                                             lambda: partial_fill)

        self.exchange._user_stream_tracker._user_stream = mock_user_stream

        self.test_task = self.local_event_loop.create_task(self.exchange._user_stream_event_listener())
        await self.resume_test_event.wait()

        self.assertEqual(1, len(self.order_filled_logger.event_log))
        fill_event: OrderFilledEvent = self.order_filled_logger.event_log[0]
        self.assertEqual(Decimal("0"), fill_event.trade_fee.percent)
        self.assertEqual(
            [TokenAmount(partial_fill["o"]["N"], Decimal(partial_fill["o"]["n"]))], fill_event.trade_fee.flat_fees
        )

        repeated_partial_fill = {
            "e": "ORDER_TRADE_UPDATE",
            "E": 1568879465651,
            "T": 1568879465650,
            "o": {
                "s": self.trading_pair,
                "c": order.client_order_id,
                "S": "BUY",
                "o": "TRAILING_STOP_MARKET",
                "f": "GTC",
                "q": "1",
                "p": "10000",
                "ap": "0",
                "sp": "7103.04",
                "x": "TRADE",
                "X": "PARTIALLY_FILLED",
                "i": 8886774,
                "l": "0.1",
                "z": "0.1",
                "L": "10000",
                "N": self.quote_asset,
                "n": "20",
                "T": 1568879465651,
                "t": 1,
                "b": "0",
                "a": "9.91",
                "m": False,
                "R": False,
                "wt": "CONTRACT_PRICE",
                "ot": "TRAILING_STOP_MARKET",
                "ps": "LONG",
                "cp": False,
                "AP": "7476.89",
                "cr": "5.0",
                "rp": "0"
            }
        }

        self.resume_test_event = asyncio.Event()
        mock_user_stream.get.side_effect = functools.partial(self._return_calculation_and_set_done_event,
                                                             lambda: repeated_partial_fill)

        self.test_task = self.local_event_loop.create_task(self.exchange._user_stream_event_listener())
        await self.resume_test_event.wait()

        self.assertEqual(1, len(self.order_filled_logger.event_log))

        self.assertEqual(0, len(self.buy_order_completed_logger.event_log))

    @aioresponses()
    async def test_user_stream_position_update_new_position(self, req_mock):

        self._simulate_trading_rules_initialized()

        position = [{'breakEvenPrice': '117.97',
                     'cumulativeFundingPayment': '0',
                     'cumulativeInterest': '0',
                     'entryPrice': '117.97',
                     'estLiquidationPrice': '0',
                     'imf': '0.02',
                     'imfFunction': {'base': '0.02', 'factor': '0.00015', 'type': 'sqrt'},
                     'markPrice': '117.97904761', 'mmf': '0.0125',
                     'mmfFunction': {'base': '0.0125', 'factor': '0.000092', 'type': 'sqrt'},
                     'netCost': '11.797',
                     'netExposureNotional': '11.797904761',
                     'netExposureQuantity': '0.1',
                     'netQuantity': '0.1',
                     'pnlRealized': '0',
                     'pnlUnrealized': '0.000904',
                     'positionId': '1530673424',
                     'subaccountId': None,
                     'symbol': self.symbol,
                     'userId': 1728445}]


        url = web_utils.public_rest_url(CONSTANTS.POSITIONS_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))

        req_mock.get(regex_url, body=json.dumps(position))

        event_message = {'data':
                             {'B': '119.38',
                              'E': 1743780556662646,
                              'M': '119.38171768',
                              'P': '0.002227',
                              'Q': '0.10',
                              'T': 1743780556662647,
                              'b': '119.35601',
                              'f': '0.02',
                              'i': 1530671598,
                              'l': '10139.422880989',
                              'm': '0.0125',
                              'n': '11.938171768',
                              'p': '-0.002399',
                              'q': '-0.10',
                              's':  self.symbol,
                              },
                         'stream': 'account.positionUpdate'}

        await self.exchange._process_user_stream_event(event_message=event_message)

        print(self.exchange.account_positions)
        self.assertEqual(1, len(self.exchange.account_positions))

    @aioresponses()
    async def test_user_stream_position_update_existing_position(self, req_mock):

        self._simulate_trading_rules_initialized()

        _position = Position(
            trading_pair=self.trading_pair,
            position_side=PositionSide.BOTH,
            unrealized_pnl=Decimal("0.01"),
            entry_price=Decimal('117.97'),
            amount=Decimal('0.2'),
            leverage=Decimal(1)
        )
        self.exchange._perpetual_trading.set_position('SOL-HBOT', _position)

        position = self.exchange.account_positions.get(self.trading_pair)
        self.assertEqual(Decimal('0.2'), position.amount)

        event_message = {'data':
                             {'B': '119.38',
                              'E': 1743780556662646,
                              'M': '119.38171768',
                              'P': '0.002227',
                              'Q': '0.10',
                              'T': 1743780556662647,
                              'b': '119.35601',
                              'f': '0.02',
                              'i': 1530671598,
                              'l': '10139.422880989',
                              'm': '0.0125',
                              'n': '11.938171768',
                              'p': '-0.002399',
                              'q': '-0.10',
                              's': self.symbol,
                              },
                         'stream': 'account.positionUpdate'}

        await self.exchange._process_user_stream_event(event_message=event_message)

        print(self.exchange.account_positions)
        self.assertEqual(1, len(self.exchange.account_positions))

        position = self.exchange.account_positions.get(self.trading_pair)
        print(position)
        self.assertEqual(Decimal('-0.1'), position.amount)

    async def test_fee_is_zero_when_not_included_in_fill_event(self):
        self.exchange.start_tracking_order(
            order_id="11113333",
            exchange_order_id="8886774",
            trading_pair=self.trading_pair,
            trade_type=TradeType.BUY,
            price=Decimal("10000"),
            amount=Decimal("1"),
            order_type=OrderType.LIMIT,
            leverage=1,
            position_action=PositionAction.OPEN,
        )

        order = self.exchange.in_flight_orders.get("11113333")

        partial_fill = {
            "e": "ORDER_TRADE_UPDATE",
            "E": 1568879465651,
            "T": 1568879465650,
            "o": {
                "s": self.trading_pair,
                "c": order.client_order_id,
                "S": "BUY",
                "o": "TRAILING_STOP_MARKET",
                "f": "GTC",
                "q": "1",
                "p": "10000",
                "ap": "0",
                "sp": "7103.04",
                "x": "TRADE",
                "X": "PARTIALLY_FILLED",
                "i": 8886774,
                "l": "0.1",
                "z": "0.1",
                "L": "10000",
                # "N": "USDT", //Do not include fee asset
                # "n": "20", //Do not include fee amount
                "T": 1568879465651,
                "t": 1,
                "b": "0",
                "a": "9.91",
                "m": False,
                "R": False,
                "wt": "CONTRACT_PRICE",
                "ot": "TRAILING_STOP_MARKET",
                "ps": "LONG",
                "cp": False,
                "AP": "7476.89",
                "cr": "5.0",
                "rp": "0"
            }

        }

        await self.exchange._process_user_stream_event(event_message=partial_fill)

        self.assertEqual(1, len(self.order_filled_logger.event_log))
        fill_event: OrderFilledEvent = self.order_filled_logger.event_log[0]
        self.assertEqual(Decimal("0"), fill_event.trade_fee.percent)
        self.assertEqual(0, len(fill_event.trade_fee.flat_fees))

    async def test_order_event_with_cancelled_status_marks_order_as_cancelled(self):
        self._simulate_trading_rules_initialized()
        self.exchange.start_tracking_order(
            order_id="11113333",
            exchange_order_id="8886774",
            trading_pair=self.trading_pair,
            trade_type=TradeType.BUY,
            price=Decimal("10000"),
            amount=Decimal("1"),
            order_type=OrderType.LIMIT,
            leverage=1,
            position_action=PositionAction.OPEN,
        )

        order = self.exchange.in_flight_orders.get("11113333")

        partial_fill = {'data':
                            {'E': 1743780565211143,
                             'O': 'USER',
                             'Q': '11.8780',
                             'S': 'Bid',
                             'T': 1743780565210485,
                             'V': 'RejectTaker',
                             'X': 'Cancelled',
                             'Z': '0',
                             'c': 11113333,
                             'e': 'orderAccepted',
                             'f': 'GTC',
                             'i': '114280403121602560',
                             'o': 'LIMIT',
                             'p': '118.78',
                             'q': '0.10',
                             'r': False,
                             's': self.symbol,
                             't': None,
                             'z': '0'},
                        'stream': 'account.orderUpdate'}


        await self.exchange._process_user_stream_event(event_message=partial_fill)


        self.assertEqual(1, len(self.order_cancelled_logger.event_log))

        self.assertTrue(self._is_logged(
            "INFO",
            f"Successfully canceled order {order.client_order_id}."
        ))

    async def test_user_stream_event_listener_raises_cancelled_error(self):
        mock_user_stream = AsyncMock()
        mock_user_stream.get.side_effect = asyncio.CancelledError

        self.exchange._user_stream_tracker._user_stream = mock_user_stream
        with self.assertRaises(asyncio.CancelledError):
            await self.exchange._user_stream_event_listener()

    @aioresponses()
    @patch("hummingbot.connector.derivative.backpack_perpetual.backpack_perpetual_derivative."
           "BackpackPerpetualDerivative.current_timestamp")
    async def test_update_order_fills_from_trades_successful(self, req_mock, mock_timestamp):
        self._simulate_trading_rules_initialized()
        self.exchange._last_poll_timestamp = 0
        mock_timestamp.return_value = 1

        self.exchange.start_tracking_order(
            order_id="11113333",
            exchange_order_id="8886774",
            trading_pair=self.trading_pair,
            trade_type=TradeType.SELL,
            price=Decimal("10000"),
            amount=Decimal("1"),
            order_type=OrderType.LIMIT,
            leverage=1,
            position_action=PositionAction.OPEN,
        )

        trades = [{
                      "clientId": "1959520749",
                      "fee": "0.002328",
                      "feeSymbol": self.quote_asset,
                      "isMaker": True,
                      "orderId": "8886774",
                      "price": "10000",
                      "quantity": "0.5",
                      "side": "Ask",
                      "symbol": self.symbol,
                      "systemOrderType": None,
                      "timestamp": "2025-04-04T10:24:08.117",
                      "tradeId": 4431977
                  },

                  ]

        url = web_utils.private_rest_url(
            CONSTANTS.ACCOUNT_TRADE_LIST_URL, domain=self.domain
        )
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))

        req_mock.get(regex_url, body=json.dumps(trades))

        await self.exchange._update_order_fills_from_trades()

        in_flight_orders = self.exchange._order_tracker.active_orders

        self.assertTrue("11113333" in in_flight_orders)

        self.assertEqual("11113333", in_flight_orders["11113333"].client_order_id)
        self.assertEqual(f"{self.base_asset}-{self.quote_asset}", in_flight_orders["11113333"].trading_pair)
        self.assertEqual(OrderType.LIMIT, in_flight_orders["11113333"].order_type)
        self.assertEqual(TradeType.SELL, in_flight_orders["11113333"].trade_type)
        self.assertEqual(10000, in_flight_orders["11113333"].price)
        self.assertEqual(1, in_flight_orders["11113333"].amount)
        self.assertEqual("8886774", in_flight_orders["11113333"].exchange_order_id)
        self.assertEqual(OrderState.PENDING_CREATE, in_flight_orders["11113333"].current_state)
        self.assertEqual(1, in_flight_orders["11113333"].leverage)
        self.assertEqual(PositionAction.OPEN, in_flight_orders["11113333"].position)

        self.assertEqual(0.5, in_flight_orders["11113333"].executed_amount_base)
        self.assertEqual(5000, in_flight_orders["11113333"].executed_amount_quote)
        self.assertEqual(1743751448.117, in_flight_orders["11113333"].last_update_timestamp)

        self.assertTrue("4431977" in in_flight_orders["11113333"].order_fills.keys())

    @aioresponses()
    async def test_update_order_fills_from_trades_failed(self, req_mock):
        self.exchange._set_current_timestamp(1640001112.0)
        self.exchange._last_poll_timestamp = 0
        self._simulate_trading_rules_initialized()
        self.exchange.start_tracking_order(
            order_id="11113333",
            exchange_order_id="8886774",
            trading_pair=self.trading_pair,
            trade_type=TradeType.SELL,
            price=Decimal("10000"),
            amount=Decimal("1"),
            order_type=OrderType.LIMIT,
            leverage=1,
            position_action=PositionAction.OPEN,
        )

        url = web_utils.private_rest_url(
            CONSTANTS.ACCOUNT_TRADE_LIST_URL, domain=self.domain
        )
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))

        req_mock.get(regex_url, exception=Exception())

        await self.exchange._update_order_fills_from_trades()

        in_flight_orders = self.exchange._order_tracker.active_orders

        # Nothing has changed
        self.assertTrue("11113333" in in_flight_orders)

        self.assertEqual("11113333", in_flight_orders["11113333"].client_order_id)
        self.assertEqual(f"{self.base_asset}-{self.quote_asset}", in_flight_orders["11113333"].trading_pair)
        self.assertEqual(OrderType.LIMIT, in_flight_orders["11113333"].order_type)
        self.assertEqual(TradeType.SELL, in_flight_orders["11113333"].trade_type)
        self.assertEqual(10000, in_flight_orders["11113333"].price)
        self.assertEqual(1, in_flight_orders["11113333"].amount)
        self.assertEqual("8886774", in_flight_orders["11113333"].exchange_order_id)
        self.assertEqual(OrderState.PENDING_CREATE, in_flight_orders["11113333"].current_state)
        self.assertEqual(1, in_flight_orders["11113333"].leverage)
        self.assertEqual(PositionAction.OPEN, in_flight_orders["11113333"].position)

        self.assertEqual(0, in_flight_orders["11113333"].executed_amount_base)
        self.assertEqual(0, in_flight_orders["11113333"].executed_amount_quote)
        self.assertEqual(1640001112.0, in_flight_orders["11113333"].last_update_timestamp)

        # Error was logged
        self.assertTrue(self._is_logged("NETWORK",
                                        f"Error fetching trades update for the order {self.trading_pair}: ."))

    @aioresponses()
    @patch("hummingbot.connector.derivative.backpack_perpetual.backpack_perpetual_derivative."
           "BackpackPerpetualDerivative.current_timestamp")
    async def test_update_order_status_successful(self, req_mock, mock_timestamp):
        self._simulate_trading_rules_initialized()
        self.exchange._last_poll_timestamp = 0
        mock_timestamp.return_value = 1

        self.exchange.start_tracking_order(
            order_id="11113333",
            exchange_order_id="8886774",
            trading_pair=self.trading_pair,
            trade_type=TradeType.SELL,
            price=Decimal("10000"),
            amount=Decimal("1"),
            order_type=OrderType.LIMIT,
            leverage=1,
            position_action=PositionAction.OPEN,
        )

        order = {
            "clientId": "11113333",
            "createdAt": 1743778854883,
            "executedQuantity": "0.5",
            "executedQuoteQuantity": "55.1",
            "id": "8886774",
            "orderType": "Limit",
            "postOnly": False,
            "price": "10000",
            "quantity": "1",
            "reduceOnly": False,
            "relatedOrderId": None,
            "selfTradePrevention": "RejectTaker",
            "side": "Bid",
            "status": "New",
            "stopLossLimitPrice": None,
            "stopLossTriggerBy": None,
            "stopLossTriggerPrice": None,
            "symbol": self.symbol,
            "takeProfitLimitPrice": None,
            "takeProfitTriggerBy": None,
            "takeProfitTriggerPrice": None,
            "timeInForce": "GTC",
            "triggerBy": None,
            "triggerPrice": None,
            "triggerQuantity": None,
            "triggeredAt": None
        }

        url = web_utils.private_rest_url(
            CONSTANTS.ORDERS_PATH_URL, domain=self.domain
        )
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))

        req_mock.get(regex_url, body=json.dumps(order))

        await self.exchange._update_order_status()
        await asyncio.sleep(0.001)

        in_flight_orders = self.exchange._order_tracker.active_orders

        self.assertTrue("11113333" in in_flight_orders)

        self.assertEqual("11113333", in_flight_orders["11113333"].client_order_id)
        self.assertEqual(f"{self.base_asset}-{self.quote_asset}", in_flight_orders["11113333"].trading_pair)
        self.assertEqual(OrderType.LIMIT, in_flight_orders["11113333"].order_type)
        self.assertEqual(TradeType.SELL, in_flight_orders["11113333"].trade_type)
        self.assertEqual(10000, in_flight_orders["11113333"].price)
        self.assertEqual(1, in_flight_orders["11113333"].amount)
        self.assertEqual("8886774", in_flight_orders["11113333"].exchange_order_id)
        self.assertEqual(OrderState.PARTIALLY_FILLED, in_flight_orders["11113333"].current_state)
        self.assertEqual(1, in_flight_orders["11113333"].leverage)
        self.assertEqual(PositionAction.OPEN, in_flight_orders["11113333"].position)

        # Processing an order update should not impact trade fill information
        self.assertEqual(Decimal("0"), in_flight_orders["11113333"].executed_amount_base)
        self.assertEqual(Decimal("0"), in_flight_orders["11113333"].executed_amount_quote)

        self.assertEqual(1743778854, int(in_flight_orders["11113333"].last_update_timestamp))

        self.assertEqual(0, len(in_flight_orders["11113333"].order_fills))

    @aioresponses()
    @patch("hummingbot.connector.derivative.backpack_perpetual.backpack_perpetual_derivative."
           "BackpackPerpetualDerivative.current_timestamp")
    async def test_request_order_status_successful(self, req_mock, mock_timestamp):
        self._simulate_trading_rules_initialized()
        self.exchange._last_poll_timestamp = 0
        mock_timestamp.return_value = 1

        self.exchange.start_tracking_order(
            order_id="11113333",
            exchange_order_id="8886774",
            trading_pair=self.trading_pair,
            trade_type=TradeType.SELL,
            price=Decimal("10000"),
            amount=Decimal("1"),
            order_type=OrderType.LIMIT,
            leverage=1,
            position_action=PositionAction.OPEN,
        )
        tracked_order = self.exchange._order_tracker.fetch_order("11113333")

        order = {"avgPrice": "0.00000",
                 "clientId": "11113333",
                 "cumQuote": "5000",
                 "executedQty": "0.5",
                 "id": 8886774,
                 "origQty": "1",
                 "origType": "LIMIT",
                 "price": "10000",
                 "reduceOnly": False,
                 "side": "SELL",
                 "positionSide": "LONG",
                 "status": "PARTIALLY_FILLED",
                 "closePosition": False,
                 "symbol": f"{self.base_asset}{self.quote_asset}",
                 "time": 1000,
                 "timeInForce": "GTC",
                 "type": "LIMIT",
                 "priceRate": "0.3",
                 "updateTime": 2000,
                 "workingType": "CONTRACT_PRICE",
                 "priceProtect": False}

        url = web_utils.private_rest_url(
            CONSTANTS.ORDERS_PATH_URL, domain=self.domain
        )
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))

        req_mock.get(regex_url, body=json.dumps(order))

        order_update = await self.exchange._request_order_status(tracked_order)

        in_flight_orders = self.exchange._order_tracker.active_orders
        self.assertTrue("11113333" in in_flight_orders)

        self.assertEqual(order_update.client_order_id, in_flight_orders["11113333"].client_order_id)
        self.assertEqual(OrderState.PARTIALLY_FILLED, order_update.new_state)
        self.assertEqual(0, len(in_flight_orders["11113333"].order_fills))

    @aioresponses()
    async def test_fetch_funding_payment_successful(self, req_mock):
        self._simulate_trading_rules_initialized()
        income_history = self._get_income_history_dict()

        url = web_utils.private_rest_url(
            CONSTANTS.GET_INCOME_HISTORY_URL, domain=self.domain
        )
        regex_url_income_history = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))

        req_mock.get(regex_url_income_history, body=json.dumps(income_history))

        funding_info = self._get_funding_info_dict()

        url = web_utils.public_rest_url(
            CONSTANTS.MARK_PRICE_URL, domain=self.domain
        )
        regex_url_funding_info = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))

        req_mock.get(regex_url_funding_info, body=json.dumps(funding_info))

        # Fetch from exchange with REST API - safe_ensure_future, not immediately
        await self.exchange._update_funding_payment(self.trading_pair, True)

        req_mock.get(regex_url_income_history, body=json.dumps(income_history))

        # Fetch once received
        await self.exchange._update_funding_payment(self.trading_pair, True)

        self.assertTrue(len(self.funding_payment_completed_logger.event_log) == 1)

        funding_info_logged = self.funding_payment_completed_logger.event_log[0]

        self.assertTrue(funding_info_logged.trading_pair == f"{self.base_asset}-{self.quote_asset}")

        self.assertEqual(funding_info_logged.funding_rate, funding_info["lastFundingRate"])
        self.assertEqual(funding_info_logged.amount, income_history[0]["income"])

    @aioresponses()
    async def test_fetch_funding_payment_failed(self, req_mock):
        self._simulate_trading_rules_initialized()
        url = web_utils.private_rest_url(
            CONSTANTS.GET_INCOME_HISTORY_URL, domain=self.domain
        )
        regex_url_income_history = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))

        req_mock.get(regex_url_income_history, exception=Exception)

        await self.exchange._update_funding_payment(self.trading_pair, False)

        self.assertTrue(self._is_logged(
            "NETWORK",
            f"Unexpected error while fetching last fee payment for {self.trading_pair}.",
        ))

    @aioresponses()
    async def test_cancel_all_successful(self, mocked_api):
        url = web_utils.private_rest_url(
            CONSTANTS.ORDERS_PATH_URL, domain=self.domain
        )
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))

        cancel_response = {"code": 200, "msg": "success", "status": "CANCELED"}
        mocked_api.delete(regex_url, body=json.dumps(cancel_response))

        self.exchange.start_tracking_order(
            order_id="11113333",
            exchange_order_id="8886774",
            trading_pair=self.trading_pair,
            trade_type=TradeType.BUY,
            price=Decimal("10000"),
            amount=Decimal("1"),
            order_type=OrderType.LIMIT,
            leverage=1,
            position_action=PositionAction.OPEN,
        )

        self.exchange.start_tracking_order(
            order_id="OID2",
            exchange_order_id="8886775",
            trading_pair=self.trading_pair,
            trade_type=TradeType.BUY,
            price=Decimal("10101"),
            amount=Decimal("1"),
            order_type=OrderType.LIMIT,
            leverage=1,
            position_action=PositionAction.OPEN,
        )

        self.assertTrue("11113333" in self.exchange._order_tracker._in_flight_orders)
        self.assertTrue("OID2" in self.exchange._order_tracker._in_flight_orders)

        cancellation_results = await self.exchange.cancel_all(timeout_seconds=1)

        order_cancelled_events = self.order_cancelled_logger.event_log

        self.assertEqual(0, len(order_cancelled_events))
        self.assertEqual(2, len(cancellation_results))

    @aioresponses()
    async def test_cancel_all_unknown_order(self, req_mock):
        self._simulate_trading_rules_initialized()
        url = web_utils.private_rest_url(
            CONSTANTS.ORDERS_PATH_URL, domain=self.domain
        )
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))

        cancel_response = {
            "code": "INVALID_CLIENT_REQUEST",
            "message": "Order not found"
        }
        req_mock.delete(regex_url, body=json.dumps(cancel_response))

        self.exchange.start_tracking_order(
            order_id="11113333",
            exchange_order_id="8886774",
            trading_pair=self.trading_pair,
            trade_type=TradeType.BUY,
            price=Decimal("10000"),
            amount=Decimal("1"),
            order_type=OrderType.LIMIT,
            leverage=1,
            position_action=PositionAction.OPEN,
        )

        tracked_order = self.exchange._order_tracker.fetch_order("11113333")
        tracked_order.current_state = OrderState.OPEN

        self.assertTrue("11113333" in self.exchange._order_tracker._in_flight_orders)

        cancellation_results = await self.exchange.cancel_all(timeout_seconds=1)

        self.assertEqual(1, len(cancellation_results))
        self.assertEqual("11113333", cancellation_results[0].order_id)

        self.assertTrue(self._is_logged(
            "DEBUG",
            "The order 11113333 does not exist on Backpack Perpetuals. "
            "No cancelation needed."
        ))

        self.assertTrue("11113333" in self.exchange._order_tracker._order_not_found_records)

    @aioresponses()
    async def test_cancel_all_error(self, req_mock):
        self._simulate_trading_rules_initialized()
        url = web_utils.private_rest_url(
            CONSTANTS.ORDERS_PATH_URL, domain=self.domain
        )
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))

        cancel_response = {
            "code": "INVALID_CLIENT_REQUEST",
            "message": "Order not found"
        }
        req_mock.delete(regex_url, body=json.dumps(cancel_response))

        self.exchange.start_tracking_order(
            order_id="11113333",
            exchange_order_id="8886774",
            trading_pair=self.trading_pair,
            trade_type=TradeType.BUY,
            price=Decimal("10000"),
            amount=Decimal("1"),
            order_type=OrderType.LIMIT,
            leverage=1,
            position_action=PositionAction.OPEN,
        )

        tracked_order = self.exchange._order_tracker.fetch_order("11113333")
        tracked_order.current_state = OrderState.OPEN

        self.assertTrue("11113333" in self.exchange._order_tracker._in_flight_orders)

        cancellation_results = await self.exchange.cancel_all(timeout_seconds=1)

        self.assertEqual(1, len(cancellation_results))
        self.assertEqual("11113333", cancellation_results[0].order_id)

        self.assertTrue(self._is_logged(
            "WARNING",
            "Failed to cancel order 11113333 (order not found)",
        ))

        self.assertTrue("11113333" in self.exchange._order_tracker._in_flight_orders)

    @aioresponses()
    async def test_cancel_all_exception(self, req_mock):
        self._simulate_trading_rules_initialized()
        url = web_utils.private_rest_url(
            CONSTANTS.ORDERS_PATH_URL, domain=self.domain
        )
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))

        req_mock.delete(regex_url, exception=Exception())

        self.exchange.start_tracking_order(
            order_id="11113333",
            exchange_order_id="8886774",
            trading_pair=self.trading_pair,
            trade_type=TradeType.BUY,
            price=Decimal("10000"),
            amount=Decimal("1"),
            order_type=OrderType.LIMIT,
            leverage=1,
            position_action=PositionAction.OPEN,
        )

        tracked_order = self.exchange._order_tracker.fetch_order("11113333")
        tracked_order.current_state = OrderState.OPEN

        self.assertTrue("11113333" in self.exchange._order_tracker._in_flight_orders)

        cancellation_results = await self.exchange.cancel_all(timeout_seconds=1)

        self.assertEqual(1, len(cancellation_results))
        self.assertEqual("11113333", cancellation_results[0].order_id)

        self.assertTrue(self._is_logged(
            "ERROR",
            "Failed to cancel order 11113333",
        ))

        self.assertTrue("11113333" in self.exchange._order_tracker._in_flight_orders)

    @aioresponses()
    async def test_cancel_order_successful(self, mock_api):
        self._simulate_trading_rules_initialized()
        url = web_utils.private_rest_url(
            CONSTANTS.ORDERS_PATH_URL, domain=self.domain
        )
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))

        cancel_response = {
            "clientId": "11113333",
            "createdAt": 1743777385876,
            "executedQuantity": "0",
            "executedQuoteQuantity": "0",
            "id": "8886774",
            "orderType": "Limit",
            "postOnly": False,
            "price": "10000",
            "quantity": "1",
            "reduceOnly": False,
            "relatedOrderId": None,
            "selfTradePrevention": "RejectTaker",
            "side": "Bid",
            "status": "Cancelled",
            "stopLossLimitPrice": None,
            "stopLossTriggerBy": None,
            "stopLossTriggerPrice": None,
            "symbol": self.symbol,
            "takeProfitLimitPrice": None,
            "takeProfitTriggerBy": None,
            "takeProfitTriggerPrice": None,
            "timeInForce": "GTC",
            "triggerBy": None,
            "triggerPrice": None,
            "triggerQuantity": None,
            "triggeredAt": None
        }
        mock_api.delete(regex_url, body=json.dumps(cancel_response))

        self.exchange.start_tracking_order(
            order_id="11113333",
            exchange_order_id="8886774",
            trading_pair=self.trading_pair,
            trade_type=TradeType.BUY,
            price=Decimal("10000"),
            amount=Decimal("1"),
            order_type=OrderType.LIMIT,
            leverage=1,
            position_action=PositionAction.OPEN,
        )
        tracked_order = self.exchange._order_tracker.fetch_order("11113333")
        tracked_order.current_state = OrderState.OPEN

        self.assertTrue("11113333" in self.exchange._order_tracker._in_flight_orders)

        canceled_order_id = await self.exchange._execute_cancel(trading_pair=self.trading_pair, order_id="11113333")
        await asyncio.sleep(0.01)

        order_cancelled_events = self.order_cancelled_logger.event_log

        self.assertEqual(1, len(order_cancelled_events))
        self.assertEqual("11113333", canceled_order_id)


    @aioresponses()
    async def test_cancel_order_error(self, mock_api):
        self._simulate_trading_rules_initialized()
        url = web_utils.private_rest_url(
            CONSTANTS.ORDERS_PATH_URL, domain=self.domain
        )
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))

        cancel_response = {
            "code": "INVALID_CLIENT_REQUEST",
            "message": "Order not found"
        }
        mock_api.delete(regex_url, body=json.dumps(cancel_response))

        self.exchange.start_tracking_order(
            order_id="11113333",
            exchange_order_id="8886774",
            trading_pair=self.trading_pair,
            trade_type=TradeType.BUY,
            price=Decimal("10000"),
            amount=Decimal("1"),
            order_type=OrderType.LIMIT,
            leverage=1,
            position_action=PositionAction.OPEN,
        )
        tracked_order = self.exchange._order_tracker.fetch_order("11113333")
        tracked_order.current_state = OrderState.OPEN

        self.assertTrue("11113333" in self.exchange._order_tracker._in_flight_orders)

        canceled_order_id = await self.exchange._execute_cancel(trading_pair=self.trading_pair, order_id="11113333")
        await asyncio.sleep(0.01)

        order_cancelled_events = self.order_cancelled_logger.event_log

        self.assertEqual(0, len(order_cancelled_events))

    @aioresponses()
    async def test_cancel_order_failed(self, mock_api):
        self._simulate_trading_rules_initialized()
        url = web_utils.private_rest_url(
            CONSTANTS.ORDERS_PATH_URL, domain=self.domain
        )


        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))

        cancel_response = {'clientId': '11113333',
                           'createdAt': 1743598676809,
                           'executedQuantity': '0.1',
                           'executedQuoteQuantity': '12.649',
                           'id': '8886774',
                           'orderType': 'Limit',
                           'postOnly': False,
                           'price': '10000',
                           'quantity': '1',
                           'reduceOnly': None,
                           'relatedOrderId': None,
                           'selfTradePrevention': 'RejectTaker',
                           'side': 'Bid',
                           'status': 'Filled',
                           'stopLossLimitPrice': None,
                           'stopLossTriggerBy': None,
                           'stopLossTriggerPrice': None,
                           'symbol': self.base_asset + '_' + self.quote_asset + '_PERP',
                           'takeProfitLimitPrice': None,
                           'takeProfitTriggerBy': None,
                           'takeProfitTriggerPrice': None,
                           'timeInForce': 'GTC',
                           'triggerBy': None,
                           'triggerPrice': None,
                           'triggerQuantity': None,
                           'triggeredAt': None
                           }
        mock_api.delete(regex_url, body=json.dumps(cancel_response))

        self.exchange.start_tracking_order(
            order_id="11113333",
            exchange_order_id="8886774",
            trading_pair=self.trading_pair,
            trade_type=TradeType.BUY,
            price=Decimal("10000"),
            amount=Decimal("1"),
            order_type=OrderType.LIMIT,
            leverage=1,
            position_action=PositionAction.OPEN,
        )
        tracked_order = self.exchange._order_tracker.fetch_order("11113333")
        tracked_order.current_state = OrderState.OPEN

        self.assertTrue("11113333" in self.exchange._order_tracker._in_flight_orders)

        await self.exchange._execute_cancel(trading_pair=self.trading_pair, order_id="11113333")

        order_cancelled_events = self.order_cancelled_logger.event_log

        self.assertEqual(0, len(order_cancelled_events))

    @aioresponses()
    async def test_create_order_successful(self, req_mock):
        url = web_utils.private_rest_url(
            CONSTANTS.ORDERS_PATH_URL, domain=self.domain
        )
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))

        create_response = {"createdAt": int(self.start_timestamp),
                           "status": "New",
                           "id": "8886774"}
        req_mock.post(regex_url, body=json.dumps(create_response))
        self._simulate_trading_rules_initialized()

        await self.exchange._create_order(
            trade_type=TradeType.BUY,
            order_id="11113333",
            trading_pair=self.trading_pair,
            amount=Decimal("10000"),
            order_type=OrderType.LIMIT,
            position_action=PositionAction.OPEN,
            price=Decimal("10000"))

        self.assertTrue("11113333" in self.exchange._order_tracker._in_flight_orders)

    @aioresponses()
    @patch("hummingbot.connector.derivative.backpack_perpetual.backpack_perpetual_web_utils.get_current_server_time")
    async def test_place_order_manage_server_overloaded_error_unkown_order(self, mock_api, mock_seconds_counter: MagicMock):
        mock_seconds_counter.return_value = 1640780000
        self.exchange._set_current_timestamp(1640780000)
        self.exchange._last_poll_timestamp = (self.exchange.current_timestamp -
                                              self.exchange.UPDATE_ORDER_STATUS_MIN_INTERVAL - 1)
        url = web_utils.private_rest_url(
            CONSTANTS.ORDERS_PATH_URL, domain=self.domain
        )
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))

        mock_response = {"code": -1003, "msg": "Unknown error, please check your request or try again later."}

        mock_api.post(regex_url, body=json.dumps(mock_response), status=503)
        self._simulate_trading_rules_initialized()

        o_id, timestamp = await self.exchange._place_order(
            trade_type=TradeType.BUY,
            order_id="11113333",
            trading_pair=self.trading_pair,
            amount=Decimal("10000"),
            order_type=OrderType.LIMIT,
            position_action=PositionAction.OPEN,
            price=Decimal("10000"))
        self.assertEqual(o_id, "UNKNOWN")

    @aioresponses()
    async def test_create_limit_maker_successful(self, req_mock):
        url = web_utils.private_rest_url(
            CONSTANTS.ORDERS_PATH_URL, domain=self.domain
        )
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))

        create_response = {"updateTime": int(self.start_timestamp),
                           "status": "NEW",
                           "id": "8886774"}
        req_mock.post(regex_url, body=json.dumps(create_response))
        self._simulate_trading_rules_initialized()

        await self.exchange._create_order(
            trade_type=TradeType.BUY,
            order_id="11113333",
            trading_pair=self.trading_pair,
            amount=Decimal("10000"),
            order_type=OrderType.LIMIT_MAKER,
            position_action=PositionAction.OPEN,
            price=Decimal("10000"))

        self.assertTrue("11113333" in self.exchange._order_tracker._in_flight_orders)

    @aioresponses()
    async def test_create_order_exception(self, req_mock):
        url = web_utils.private_rest_url(
            CONSTANTS.ORDERS_PATH_URL, domain=self.domain
        )
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        req_mock.post(regex_url, exception=Exception())
        self._simulate_trading_rules_initialized()
        await self.exchange._create_order(
            trade_type=TradeType.BUY,
            order_id="11113333",
            trading_pair=self.trading_pair,
            amount=Decimal("10000"),
            order_type=OrderType.LIMIT,
            position_action=PositionAction.OPEN,
            price=Decimal("1010"))
        await asyncio.sleep(0.001)

        self.assertTrue("11113333" not in self.exchange._order_tracker._in_flight_orders)

        # The order amount is quantizied
        # "Error submitting buy LIMIT order to Binance_perpetual for 9999 COINALPHA-HBOT 1010."
        self.assertTrue(self._is_logged(
            "NETWORK",
            f"Error submitting {TradeType.BUY.name.lower()} {OrderType.LIMIT.name.upper()} order to {self.exchange.name_cap} for "
            f"{Decimal('9999')} {self.trading_pair} {Decimal('1010')}.",
        ))

    async def test_create_order_min_order_size_failure(self):
        self._simulate_trading_rules_initialized()
        margin_asset = self.quote_asset
        min_order_size = 3
        mocked_response = self._get_exchange_info_mock_response(margin_asset, min_order_size=min_order_size)
        trading_rules = await self.exchange._format_trading_rules(mocked_response)
        self.exchange._trading_rules[self.trading_pair] = trading_rules[0]
        trade_type = TradeType.BUY
        amount = Decimal("2")

        await self.exchange._create_order(
            trade_type=trade_type,
            order_id="11113333",
            trading_pair=self.trading_pair,
            amount=amount,
            order_type=OrderType.LIMIT,
            position_action=PositionAction.OPEN,
            price=Decimal("1010"))

        await asyncio.sleep(0.001)

        self.assertTrue("11113333" not in self.exchange._order_tracker._in_flight_orders)

        self.assertTrue(self._is_logged(
            "WARNING",
            f"{trade_type.name.title()} order amount {amount} is lower than the minimum order "
            f"size {trading_rules[0].min_order_size}. The order will not be created, increase the "
            f"amount to be higher than the minimum order size."
        ))

    async def test_create_order_min_notional_size_failure(self):
        margin_asset = self.quote_asset
        min_notional_size = 10
        self._simulate_trading_rules_initialized()
        mocked_response = self._get_exchange_info_mock_response(margin_asset,
                                                                min_notional_size=min_notional_size,
                                                                min_base_amount_increment=0.5)
        trading_rules = await self.exchange._format_trading_rules(mocked_response)
        self.exchange._trading_rules[self.trading_pair] = trading_rules[0]
        trade_type = TradeType.BUY
        amount = Decimal("2")
        price = Decimal("4")

        await self.exchange._create_order(
            trade_type=trade_type,
            order_id="11113333",
            trading_pair=self.trading_pair,
            amount=amount,
            order_type=OrderType.LIMIT,
            position_action=PositionAction.OPEN,
            price=price)
        await asyncio.sleep(0.001)

        self.assertTrue("11113333" not in self.exchange._order_tracker._in_flight_orders)

    async def test_restore_tracking_states_only_registers_open_orders(self):
        orders = []
        orders.append(InFlightOrder(
            client_order_id="11113333",
            exchange_order_id="E11113333",
            trading_pair=self.trading_pair,
            order_type=OrderType.LIMIT,
            trade_type=TradeType.BUY,
            amount=Decimal("1000.0"),
            price=Decimal("1.0"),
            creation_timestamp=1640001112.223,
        ))
        orders.append(InFlightOrder(
            client_order_id="OID2",
            exchange_order_id="EOID2",
            trading_pair=self.trading_pair,
            order_type=OrderType.LIMIT,
            trade_type=TradeType.BUY,
            amount=Decimal("1000.0"),
            price=Decimal("1.0"),
            creation_timestamp=1640001112.223,
            initial_state=OrderState.CANCELED
        ))
        orders.append(InFlightOrder(
            client_order_id="OID3",
            exchange_order_id="EOID3",
            trading_pair=self.trading_pair,
            order_type=OrderType.LIMIT,
            trade_type=TradeType.BUY,
            amount=Decimal("1000.0"),
            price=Decimal("1.0"),
            creation_timestamp=1640001112.223,
            initial_state=OrderState.FILLED
        ))
        orders.append(InFlightOrder(
            client_order_id="OID4",
            exchange_order_id="EOID4",
            trading_pair=self.trading_pair,
            order_type=OrderType.LIMIT,
            trade_type=TradeType.BUY,
            amount=Decimal("1000.0"),
            price=Decimal("1.0"),
            creation_timestamp=1640001112.223,
            initial_state=OrderState.FAILED
        ))

        tracking_states = {order.client_order_id: order.to_json() for order in orders}

        self.exchange.restore_tracking_states(tracking_states)

        self.assertIn("11113333", self.exchange.in_flight_orders)
        self.assertNotIn("OID2", self.exchange.in_flight_orders)
        self.assertNotIn("OID3", self.exchange.in_flight_orders)
        self.assertNotIn("OID4", self.exchange.in_flight_orders)


    @aioresponses()
    async def test_update_balances(self, mock_api):
        url = web_utils.public_rest_url(CONSTANTS.SERVER_TIME_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))

        response = {"serverTime": 1640000003000}

        mock_api.get(regex_url,
                     body=json.dumps(response))

        url = web_utils.private_rest_url(CONSTANTS.ACCOUNT_INFO_URL, domain=self.domain)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))

        response = {
            "feeTier": 0,
            "canTrade": True,
            "canDeposit": True,
            "canWithdraw": True,
            "updateTime": 0,
            "totalInitialMargin": "0.00000000",
            "totalMaintMargin": "0.00000000",
            "totalWalletBalance": "23.72469206",
            "totalUnrealizedProfit": "0.00000000",
            "totalMarginBalance": "23.72469206",
            "totalPositionInitialMargin": "0.00000000",
            "totalOpenOrderInitialMargin": "0.00000000",
            "totalCrossWalletBalance": "23.72469206",
            "totalCrossUnPnl": "0.00000000",
            "availableBalance": "23.72469206",
            "maxWithdrawAmount": "23.72469206",
            "assets": [
                {
                    "asset": "USDT",
                    "walletBalance": "23.72469206",
                    "unrealizedProfit": "0.00000000",
                    "marginBalance": "23.72469206",
                    "maintMargin": "0.00000000",
                    "initialMargin": "0.00000000",
                    "positionInitialMargin": "0.00000000",
                    "openOrderInitialMargin": "0.00000000",
                    "crossWalletBalance": "23.72469206",
                    "crossUnPnl": "0.00000000",
                    "availableBalance": "23.72469206",
                    "maxWithdrawAmount": "23.72469206",
                    "marginAvailable": True,
                    "updateTime": 1625474304765,
                },
                {
                    "asset": "BUSD",
                    "walletBalance": "103.12345678",
                    "unrealizedProfit": "0.00000000",
                    "marginBalance": "103.12345678",
                    "maintMargin": "0.00000000",
                    "initialMargin": "0.00000000",
                    "positionInitialMargin": "0.00000000",
                    "openOrderInitialMargin": "0.00000000",
                    "crossWalletBalance": "103.12345678",
                    "crossUnPnl": "0.00000000",
                    "availableBalance": "100.12345678",
                    "maxWithdrawAmount": "103.12345678",
                    "marginAvailable": True,
                    "updateTime": 1625474304765,
                }
            ],
            "positions": [{
                "symbol": "BTCUSDT",
                "initialMargin": "0",
                "maintMargin": "0",
                "unrealizedProfit": "0.00000000",
                "positionInitialMargin": "0",
                "openOrderInitialMargin": "0",
                "leverage": "100",
                "isolated": True,
                "entryPrice": "0.00000",
                "maxNotional": "250000",
                "bidNotional": "0",
                "askNotional": "0",
                "positionSide": "BOTH",
                "positionAmt": "0",
                "updateTime": 0,
            }
            ]
        }

        mock_api.get(regex_url, body=json.dumps(response))
        await self.exchange._update_balances()

        available_balances = self.exchange.available_balances
        total_balances = self.exchange.get_all_balances()

        self.assertEqual(Decimal("23.72469206"), available_balances["USDT"])
        self.assertEqual(Decimal("100.12345678"), available_balances["BUSD"])
        self.assertEqual(Decimal("23.72469206"), total_balances["USDT"])
        self.assertEqual(Decimal("103.12345678"), total_balances["BUSD"])

    @aioresponses()
    @patch("hummingbot.connector.time_synchronizer.TimeSynchronizer._current_seconds_counter")
    async def test_account_info_request_includes_timestamp(self, mock_api, mock_seconds_counter):
        mock_seconds_counter.return_value = 1000

        url = web_utils.public_rest_url(CONSTANTS.SERVER_TIME_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))

        response = {"serverTime": 1640000003000}

        mock_api.get(regex_url,
                     body=json.dumps(response))

        url = web_utils.private_rest_url(CONSTANTS.ACCOUNT_INFO_URL, domain=self.domain)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))

        response = {
            "feeTier": 0,
            "canTrade": True,
            "canDeposit": True,
            "canWithdraw": True,
            "updateTime": 0,
            "totalInitialMargin": "0.00000000",
            "totalMaintMargin": "0.00000000",
            "totalWalletBalance": "23.72469206",
            "totalUnrealizedProfit": "0.00000000",
            "totalMarginBalance": "23.72469206",
            "totalPositionInitialMargin": "0.00000000",
            "totalOpenOrderInitialMargin": "0.00000000",
            "totalCrossWalletBalance": "23.72469206",
            "totalCrossUnPnl": "0.00000000",
            "availableBalance": "23.72469206",
            "maxWithdrawAmount": "23.72469206",
            "assets": [
                {
                    "asset": "USDT",
                    "walletBalance": "23.72469206",
                    "unrealizedProfit": "0.00000000",
                    "marginBalance": "23.72469206",
                    "maintMargin": "0.00000000",
                    "initialMargin": "0.00000000",
                    "positionInitialMargin": "0.00000000",
                    "openOrderInitialMargin": "0.00000000",
                    "crossWalletBalance": "23.72469206",
                    "crossUnPnl": "0.00000000",
                    "availableBalance": "23.72469206",
                    "maxWithdrawAmount": "23.72469206",
                    "marginAvailable": True,
                    "updateTime": 1625474304765,
                },
                {
                    "asset": "BUSD",
                    "walletBalance": "103.12345678",
                    "unrealizedProfit": "0.00000000",
                    "marginBalance": "103.12345678",
                    "maintMargin": "0.00000000",
                    "initialMargin": "0.00000000",
                    "positionInitialMargin": "0.00000000",
                    "openOrderInitialMargin": "0.00000000",
                    "crossWalletBalance": "103.12345678",
                    "crossUnPnl": "0.00000000",
                    "availableBalance": "100.12345678",
                    "maxWithdrawAmount": "103.12345678",
                    "marginAvailable": True,
                    "updateTime": 1625474304765,
                }
            ],
            "positions": [{
                "symbol": "BTCUSDT",
                "initialMargin": "0",
                "maintMargin": "0",
                "unrealizedProfit": "0.00000000",
                "positionInitialMargin": "0",
                "openOrderInitialMargin": "0",
                "leverage": "100",
                "isolated": True,
                "entryPrice": "0.00000",
                "maxNotional": "250000",
                "bidNotional": "0",
                "askNotional": "0",
                "positionSide": "BOTH",
                "positionAmt": "0",
                "updateTime": 0,
            }
            ]
        }

        mock_api.get(regex_url, body=json.dumps(response))
        await self.exchange._update_balances()

        account_request = next(((key, value) for key, value in mock_api.requests.items()
                                if key[1].human_repr().startswith(url)))
        request_params = account_request[1][0].kwargs["params"]
        self.assertIsInstance(request_params["timestamp"], int)

    async def test_limit_orders(self):
        self.exchange.start_tracking_order(
            order_id="11113333",
            exchange_order_id="8886774",
            trading_pair=self.trading_pair,
            trade_type=TradeType.BUY,
            price=Decimal("10000"),
            amount=Decimal("1"),
            order_type=OrderType.LIMIT,
            leverage=1,
            position_action=PositionAction.OPEN,
        )
        self.exchange.start_tracking_order(
            order_id="OID2",
            exchange_order_id="8886774",
            trading_pair=self.trading_pair,
            trade_type=TradeType.BUY,
            price=Decimal("10000"),
            amount=Decimal("1"),
            order_type=OrderType.LIMIT,
            leverage=1,
            position_action=PositionAction.OPEN,
        )

        limit_orders = self.exchange.limit_orders

        self.assertEqual(len(limit_orders), 2)
        self.assertIsInstance(limit_orders, list)
        self.assertIsInstance(limit_orders[0], LimitOrder)

    def _simulate_trading_rules_initialized(self):
        margin_asset = self.quote_asset
        mocked_response = self._get_exchange_info_mock_response(margin_asset)
        self.exchange._initialize_trading_pair_symbols_from_exchange_info(mocked_response)
        self.exchange._trading_rules = {
            self.trading_pair: TradingRule(
                trading_pair=self.trading_pair,
                min_order_size=Decimal(str(1)),
                min_price_increment=Decimal(str(2)),
                min_base_amount_increment=Decimal(str(3)),
                min_notional_size=Decimal(str(4)),
            )
        }
        return self.exchange._trading_rules
