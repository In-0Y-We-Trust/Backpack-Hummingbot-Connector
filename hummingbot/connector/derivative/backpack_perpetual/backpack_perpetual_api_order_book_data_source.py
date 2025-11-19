import asyncio
import time
from collections import defaultdict
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional

import hummingbot.connector.derivative.backpack_perpetual.backpack_perpetual_constants as CONSTANTS
from hummingbot.core.data_type.funding_info import FundingInfo, FundingInfoUpdate
from hummingbot.core.data_type.order_book_message import OrderBookMessage, OrderBookMessageType
from hummingbot.core.data_type.perpetual_api_order_book_data_source import PerpetualAPIOrderBookDataSource
from hummingbot.core.web_assistant.connections.data_types import WSJSONRequest
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory
from hummingbot.core.web_assistant.ws_assistant import WSAssistant
from hummingbot.logger import HummingbotLogger

if TYPE_CHECKING:
    from hummingbot.connector.derivative.backpack_perpetual.backpack_perpetual_derivative import (
        BackpackPerpetualDerivative,
    )


class BackpackPerpetualAPIOrderBookDataSource(PerpetualAPIOrderBookDataSource):
    _bpobds_logger: Optional[HummingbotLogger] = None
    _trading_pair_symbol_map: Dict[str, Mapping[str, str]] = {}
    _mapping_initialization_lock = asyncio.Lock()

    def __init__(
            self,
            trading_pairs: List[str],
            connector: 'BackpackPerpetualDerivative',
            api_factory: WebAssistantsFactory
    ):
        super().__init__(trading_pairs)
        self._connector = connector
        self._api_factory = api_factory
        self._trading_pairs: List[str] = trading_pairs
        self._message_queue: Dict[str, asyncio.Queue] = defaultdict(asyncio.Queue)
        self._trade_messages_queue_key = CONSTANTS.TRADE_STREAM_ID
        self._diff_messages_queue_key = CONSTANTS.DIFF_STREAM_ID
        self._funding_info_messages_queue_key = CONSTANTS.FUNDING_INFO_STREAM_ID
        self._snapshot_messages_queue_key = "order_book_snapshot"

    async def get_last_traded_prices(self,  trading_pairs: List[str], domain: Optional[str] = None) -> Dict[str, float]:
        return await self._connector.get_last_traded_prices(trading_pairs=trading_pairs)

    async def get_funding_info(self, trading_pair: str) -> FundingInfo:
        symbol_info: Dict[str, Any] = await self._request_complete_funding_info(trading_pair)
        """
        [
              {
                    "fundingRate": "-0.000042897",
                    "indexPrice": "128.84331161",
                    "markPrice": "128.78918685",
                    "nextFundingTimestamp": 1743609600000,
                    "symbol": "SOL_USDC_PERP"
                }
            ]
            """
        funding_info = FundingInfo(
            trading_pair=trading_pair,
            index_price=Decimal(symbol_info["indexPrice"]),
            mark_price=Decimal(symbol_info["markPrice"]),
            next_funding_utc_timestamp=int(float(symbol_info["nextFundingTimestamp"]) * 1e-3),
            rate=Decimal(symbol_info["fundingRate"]),
        )
        return funding_info

    async def _request_order_book_snapshot(self, trading_pair: str) -> Dict[str, Any]:
        ex_trading_pair = await self._connector.exchange_symbol_associated_to_pair(trading_pair=trading_pair)

        params = {
            "symbol": ex_trading_pair
        }

        data = await self._connector._api_get(
            path_url=CONSTANTS.ORDER_BOOK_PATH_URL,
            params=params)
        return data

    async def _order_book_snapshot(self, trading_pair: str) -> OrderBookMessage:
        snapshot_response: Dict[str, Any] = await self._request_order_book_snapshot(trading_pair)
        """
                {
        "asks": [
            [
            "21.9",
            "500.123"
            ],
            [
            "22.1",
            "2321.11"
            ]
        ],
        "bids": [
            [
            "20.12",
            "255.123"
            ],
            [
            "20.5",
            "499.555"
            ]
        ],
        "lastUpdateId": "1684026955123",
        "timestamp": 1684026955123
        }
        """
        snapshot_timestamp: float = snapshot_response["timestamp"] * 1e-3
        snapshot_msg: OrderBookMessage = OrderBookMessage(OrderBookMessageType.SNAPSHOT, {
            "trading_pair": trading_pair,
            "update_id": int(snapshot_response["lastUpdateId"]),
            "bids": snapshot_response["bids"],
            "asks": snapshot_response["asks"]
        }, timestamp=snapshot_timestamp)
        return snapshot_msg

    async def _connected_websocket_assistant(self) -> WSAssistant:
        url = "wss://ws.backpack.exchange"
        ws: WSAssistant = await self._api_factory.get_ws_assistant()
        await ws.connect(ws_url=url, ping_timeout=CONSTANTS.HEARTBEAT_TIME_INTERVAL)
        return ws

    async def _subscribe_channels(self, ws: WSAssistant):
        """
        Subscribes to the trade events and diff orders events through the provided websocket connection.
        :param ws: the websocket assistant used to connect to the exchange
        """
        try:
            channels = [
                "markPrice.",
                "depth.200ms."
            ]
            params = []
            for channel in channels:
                for trading_pair in self._trading_pairs:
                    symbol = await self._connector.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
                    params.append(f"{channel}{symbol}")
            
            payload = {
                "method": "SUBSCRIBE",
                "params": params
            }
            subscribe_request: WSJSONRequest = WSJSONRequest(payload)
            await ws.send(subscribe_request)   
            self.logger().info("Subscribed to public order book info channels...")
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger().exception("Unexpected error occurred subscribing to order book trading and delta streams...")
            raise

    def _channel_originating_message(self, event_message: Dict[str, Any]) -> str:
        channel = ""
        if "result" not in event_message:
            stream_name = event_message.get("stream")
            if "depth" in stream_name:
                channel = self._diff_messages_queue_key
            elif "markPrice" in stream_name:
                channel = self._funding_info_messages_queue_key
        return channel

    async def _parse_order_book_diff_message(self, raw_message: Dict[str, Any], message_queue: asyncio.Queue):
        """
        {
                "e": "depth",           // Event type
                "E": 1694687965941000,  // Event time in microseconds
                "s": "SOL_USDC",        // Symbol
                "a": [                  // Asks
                    [
                    "18.70",
                    "0.000"
                    ]
                ],
                "b": [                  // Bids
                    [
                    "18.67",
                    "0.832"
                    ],
                    [
                    "18.68",
                    "0.000"
                    ]
                ],
                "U": 94978271,          // First update ID in event
                "u": 94978271,          // Last update ID in event
                "T": 1694687965940999   // Engine timestamp in microseconds
                }
        """
        data = raw_message["data"]
        timestamp: float = data["E"] * 1e-3
        trading_pair = await self._connector.trading_pair_associated_to_exchange_symbol(data["s"])
        order_book_message: OrderBookMessage = OrderBookMessage(OrderBookMessageType.DIFF, {
            "trading_pair": trading_pair,
            "update_id": data["u"],
            "bids": data["b"],
            "asks": data["a"]
        }, timestamp=timestamp)
        message_queue.put_nowait(order_book_message)

    async def _parse_trade_message(self, raw_message: Dict[str, Any], message_queue: asyncio.Queue):
       # TODO do not subscribed
       pass

    async def listen_for_order_book_snapshots(self, ev_loop: asyncio.BaseEventLoop, output: asyncio.Queue):
        while True:
            try:
                for trading_pair in self._trading_pairs:
                    snapshot_msg: OrderBookMessage = await self._order_book_snapshot(trading_pair)
                    output.put_nowait(snapshot_msg)
                    self.logger().debug(f"Saved order book snapshot for {trading_pair}")
                delta = CONSTANTS.ONE_HOUR - time.time() % CONSTANTS.ONE_HOUR
                await self._sleep(delta)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger().error(
                    "Unexpected error occurred fetching orderbook snapshots. Retrying in 5 seconds...", exc_info=True
                )
                await self._sleep(5.0)

    async def _parse_funding_info_message(self, raw_message: Dict[str, Any], message_queue: asyncio.Queue):
        """
        {
        "e": "markPrice",           // Event type
        "E": 1694687965941000,      // Event time in microseconds
        "s": "SOL_USDC",            // Symbol
        "p": "18.70",               // Mark price
        "f": "1.70",                // Estimated funding rate
        "i": "19.70",               // Index price
        "n": 1694687965941000,      // Next funding timestamp in microseconds
        }
        """
        data: Dict[str, Any] = raw_message["data"]
        trading_pair = await self._connector.trading_pair_associated_to_exchange_symbol(data["s"])

        if trading_pair not in self._trading_pairs:
            return
        funding_info = FundingInfoUpdate(
            trading_pair=trading_pair,
            index_price=Decimal(data["i"]),
            mark_price=Decimal(data["p"]),
            next_funding_utc_timestamp=int(float(data["n"]) * 1e-3),
            rate=Decimal(data["f"]),
        )

        message_queue.put_nowait(funding_info)

    async def _request_complete_funding_info(self, trading_pair: str):
        ex_trading_pair = await self._connector.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
        data = await self._connector._api_get(
            path_url=CONSTANTS.MARK_PRICE_PATH_URL,
            params={"symbol": ex_trading_pair},
            is_auth_required=False)
        return data[0]
