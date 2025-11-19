import logging
import time
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, AsyncIterable, Dict, List, Tuple

from bidict import bidict

from hummingbot.connector.derivative.backpack_perpetual.backpack_perpetual_api_order_book_data_source import (
    BackpackPerpetualAPIOrderBookDataSource,
)
from hummingbot.connector.derivative.backpack_perpetual.backpack_perpetual_constants import MAX_ID_BIT_COUNT, \
    EXCHANGE_NAME
from hummingbot.connector.derivative.backpack_perpetual.backpack_perpetual_user_stream_tracker import (
    BackpackPerpetualUserStreamTracker,
)
from hummingbot.connector.derivative.position import Position
from hummingbot.connector.perpetual_derivative_py_base import PerpetualDerivativePyBase
from hummingbot.connector.utils import combine_to_hb_trading_pair, get_new_client_order_id, \
    get_new_numeric_client_order_id
from hummingbot.core.api_throttler.data_types import RateLimit
from hummingbot.core.data_type.order_book_tracker_data_source import OrderBookTrackerDataSource
from hummingbot.core.data_type.user_stream_tracker_data_source import UserStreamTrackerDataSource
from hummingbot.core.utils.async_utils import safe_gather, safe_ensure_future
from hummingbot.core.utils.estimate_fee import build_trade_fee
from hummingbot.core.utils.tracking_nonce import NonceCreator
from hummingbot.core.web_assistant.connections.data_types import RESTMethod
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory
from hummingbot.strategy_v2.backtesting.backtesting_data_provider import logger

if TYPE_CHECKING:
    from hummingbot.client.config.config_helpers import ClientConfigAdapter

import asyncio
from typing import Optional

from hummingbot.connector.derivative.backpack_perpetual import (
    backpack_perpetual_constants as constants,
    backpack_perpetual_web_utils as web_utils,
)
from hummingbot.connector.derivative.backpack_perpetual.backpack_perpetual_auth import BackpackPerpetualAuth
from hummingbot.connector.trading_rule import TradingRule
from hummingbot.core.data_type.common import OrderType, PositionAction, PositionMode, PositionSide, TradeType
from hummingbot.core.data_type.in_flight_order import InFlightOrder, OrderUpdate, TradeUpdate, OrderState
from hummingbot.core.data_type.trade_fee import TokenAmount, TradeFeeBase


class BackpackPerpetualDerivative(PerpetualDerivativePyBase):
    web_utils = web_utils
    SHORT_POLL_INTERVAL = 5.0
    UPDATE_ORDER_STATUS_MIN_INTERVAL = 10.0
    LONG_POLL_INTERVAL = 120.0

    def __init__(self,
                 client_config_map: "ClientConfigAdapter",
                 backpack_api_key: str = None,
                 backpack_secret_key: str = None,
                 trading_pairs: Optional[List[str]] = None,
                 trading_required: bool = True,
                 domain: str = constants.DEFAULT_DOMAIN):
        
        self.backpack_api_key = backpack_api_key
        self.backpack_secret_key = backpack_secret_key
        self._trading_required = trading_required
        self._trading_pairs = trading_pairs
        self._position_mode = PositionMode.ONEWAY
        self._leverage = Decimal("1")
        self._last_trade_history_timestamp = None
        self._domain = domain
        self._client_order_id_nonce_provider = NonceCreator.for_microseconds()
        super().__init__(client_config_map)

    @property
    def name(self) -> str:
        return EXCHANGE_NAME
    
    @property
    def authenticator(self) -> BackpackPerpetualAuth:
        return BackpackPerpetualAuth(self.backpack_api_key, self.backpack_secret_key,
                                    self._time_synchronizer)
    
    @property
    def rate_limits_rules(self) -> List[RateLimit]:
        return constants.RATE_LIMITS

    @property
    def domain(self) -> str:
        return self._domain

    @property
    def client_order_id_max_length(self) -> int:
        return constants.MAX_ORDER_ID_LEN

    @property
    def client_order_id_prefix(self) -> str:
        return "" # no broker

    @property
    def trading_rules_request_path(self) -> str:
        return constants.MARKETS_PATH_URL

    @property
    def trading_pairs_request_path(self) -> str:
        return constants.MARKETS_PATH_URL

    @property
    def check_network_request_path(self) -> str:
        return constants.STATUS_PATH_URL

    @property
    def trading_pairs(self):
        return self._trading_pairs

    @property
    def is_cancel_request_in_exchange_synchronous(self) -> bool:
        return True

    @property
    def is_trading_required(self) -> bool:
        return self._trading_required

    @property
    def funding_fee_poll_interval(self) -> int:
        return 600
    
    def supported_order_types(self) -> List[OrderType]:
        """
        return a list of OrderType supported by this connector
        """
        return [OrderType.LIMIT, OrderType.MARKET]
    
    def supported_position_modes(self) -> List[PositionMode]:
        """Get supported position modes."""
        return [PositionMode.ONEWAY]

    def get_buy_collateral_token(self, trading_pair: str) -> str:
        trading_rule: TradingRule = self._trading_rules[trading_pair]
        return trading_rule.buy_order_collateral_token

    def get_sell_collateral_token(self, trading_pair: str) -> str:
        trading_rule: TradingRule = self._trading_rules[trading_pair]
        return trading_rule.sell_order_collateral_token

    def _is_request_exception_related_to_time_synchronizer(self, request_exception: Exception):
        error_description = str(request_exception)
        is_time_synchronizer_related = ("Invalid timestamp" in error_description
                                        and "must be within" in error_description)
        return is_time_synchronizer_related

    def _is_order_not_found_during_status_update_error(self, status_update_exception: Exception) -> bool:
        return "RESOURCE_NOT_FOUND" in str(status_update_exception)

    def _is_order_not_found_during_cancelation_error(self, cancelation_exception: Exception) -> bool:
        return "Order not found" in str(cancelation_exception)

    async def _api_request(
            self,
            path_url,
            overwrite_url: Optional[str] = None,
            method: RESTMethod = RESTMethod.GET,
            params: Optional[Dict[str, Any]] = None,
            data: Optional[Dict[str, Any]] = None,
            is_auth_required: bool = False,
            return_err: bool = False,
            limit_id: Optional[str] = None,
            headers: Optional[Dict[str, Any]] = None,
            **kwargs,
    ) -> Dict[str, Any]:
        """
        Override the _api_request method to use our custom throttler limit ID mapping
        """
        if limit_id is None:
            # Map the path URL to appropriate limit ID from our mapping
            limit_id = self.web_utils.get_throttler_limit_id(path_url)
            
        return await super()._api_request(
            path_url=path_url,
            overwrite_url=overwrite_url,
            method=method,
            params=params,
            data=data,
            is_auth_required=is_auth_required,
            return_err=return_err,
            limit_id=limit_id,
            headers=headers,
            **kwargs,
        )

    def _create_web_assistants_factory(self) -> WebAssistantsFactory:
        return web_utils.build_api_factory(
            throttler=self._throttler,
            time_synchronizer=self._time_synchronizer,
            auth=self._auth)

    def _create_order_book_data_source(self) -> OrderBookTrackerDataSource:
        return BackpackPerpetualAPIOrderBookDataSource(
            trading_pairs=self._trading_pairs,
            connector=self,
            api_factory=self._web_assistants_factory
        )

    def _create_user_stream_data_source(self) -> UserStreamTrackerDataSource:
        return BackpackPerpetualUserStreamTracker(
            auth=self._auth,
            connector=self,
            api_factory=self._web_assistants_factory
        )

    def _get_fee(self,
                 base_currency: str,
                 quote_currency: str,
                 order_type: OrderType,
                 order_side: TradeType,
                 position_action: PositionAction,
                 amount: Decimal,
                 price: Decimal =  Decimal("nan"),
                 is_maker: Optional[bool] = None) -> TradeFeeBase:
        is_maker = is_maker or False
        fee = build_trade_fee(
            self.name,
            is_maker,
            base_currency=base_currency,
            quote_currency=quote_currency,
            order_type=order_type,
            order_side=order_side,
            amount=amount,
            price=price,
        )
        return fee

    async def _update_trading_fees(self):
        """
        Update fees information from the exchange
        """
        pass


    async def _status_polling_loop_fetch_updates(self):
        await safe_gather(
            self._update_order_fills_from_trades(),
            self._update_order_status(),
            self._update_balances(),
            self._update_positions(),
        )
        
    # TODO maybe add _PERP    
    # async def trading_pair_associated_to_exchange_symbol(self, symbol: str):
    #     return symbol.rstrip("-SWAP")

    # async def exchange_symbol_associated_to_pair(self, trading_pair: str):
    #     return f"{trading_pair}-SWAP"
        
    async def _place_cancel(self, order_id: str, tracked_order: InFlightOrder):
        symbol = await self.exchange_symbol_associated_to_pair(trading_pair=tracked_order.trading_pair)

        int_order_id = int(order_id)

        if str(int_order_id) != order_id:
            raise ValueError(f"Order ID {order_id} is not a valid integer.")

        api_params = {
            "clientId": int_order_id,
            "symbol": symbol,
        }

        cancel_result = await self._api_delete(
            path_url=constants.ORDERS_PATH_URL,
            data=api_params,
            is_auth_required=True)
        if cancel_result.get("code") == "INVALID_CLIENT_REQUEST" and "Order not found" == cancel_result.get("message", ""):
            self.logger().debug(f"The order {order_id} does not exist on Backpack Perpetuals. "
                                f"No cancelation needed.")
            await self._order_tracker.process_order_not_found(order_id)
            raise IOError(f"{cancel_result.get('code')} - {cancel_result['message']}")
        if cancel_result.get("status") == "Cancelled":
            return True
        return False

    def buy(self,
            trading_pair: str,
            amount: Decimal,
            order_type=OrderType.LIMIT,
            price: Decimal = Decimal("nan"),
            **kwargs) -> str:
        """
        Creates a promise to create a buy order using the parameters

        :param trading_pair: the token pair to operate with
        :param amount: the order amount
        :param order_type: the type of order to create (MARKET, LIMIT, LIMIT_MAKER)
        :param price: the order price

        :return: the id assigned by the connector to the order (the client id)
        """
        order_id = str(get_new_numeric_client_order_id(
            nonce_creator=self._client_order_id_nonce_provider,
            max_id_bit_count=MAX_ID_BIT_COUNT,
        ))
        safe_ensure_future(self._create_order(
            trade_type=TradeType.BUY,
            order_id=order_id,
            trading_pair=trading_pair,
            amount=amount,
            order_type=order_type,
            price=price,
            **kwargs))
        return order_id

    def sell(self,
             trading_pair: str,
             amount: Decimal,
             order_type: OrderType = OrderType.LIMIT,
             price: Decimal = Decimal("nan"),
             **kwargs) -> str:
        """
        Creates a promise to create a sell order using the parameters.
        :param trading_pair: the token pair to operate with
        :param amount: the order amount
        :param order_type: the type of order to create (MARKET, LIMIT, LIMIT_MAKER)
        :param price: the order price
        :return: the id assigned by the connector to the order (the client id)
        """
        order_id = str(get_new_numeric_client_order_id(
            nonce_creator=self._client_order_id_nonce_provider,
            max_id_bit_count=MAX_ID_BIT_COUNT,
        ))
        safe_ensure_future(self._create_order(
            trade_type=TradeType.SELL,
            order_id=order_id,
            trading_pair=trading_pair,
            amount=amount,
            order_type=order_type,
            price=price,
            **kwargs))
        return order_id

    async def _place_order(
            self,
            order_id: str,
            trading_pair: str,
            amount: Decimal,
            trade_type: TradeType,
            order_type: OrderType,
            price: Decimal,
            position_action: PositionAction = PositionAction.NIL,
            **kwargs,
    ) -> Tuple[str, float]:

        int_order_id = int(order_id)

        if str(int_order_id) != order_id:
            raise ValueError(f"Order ID {order_id} is not a valid integer.")

        amount_str = f"{amount:f}"
        price_str = f"{price:f}"
        symbol = await self.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
        api_params = {"symbol": symbol,
                      "side": "Bid" if trade_type is TradeType.BUY else "Ask",
                      "quantity": amount_str,
                      "orderType": "Market" if order_type is OrderType.MARKET else "Limit",
                      "clientId": int_order_id
                      }
        if order_type.is_limit_type():
            api_params["price"] = price_str
        if order_type == OrderType.LIMIT:
            api_params["timeInForce"] = "GTC"
        # if self._position_mode == PositionMode.HEDGE:
        #     if position_action == PositionAction.OPEN:
        #         api_params["positionSide"] = "LONG" if trade_type is TradeType.BUY else "SHORT"
        #     else:
        #         api_params["positionSide"] = "SHORT" if trade_type is TradeType.BUY else "LONG"
        try:
            order_result = await self._api_post(
                path_url=constants.ORDERS_PATH_URL,
                data=api_params,
                is_auth_required=True)
            o_id = str(order_result["id"])
            transact_time = order_result["createdAt"] * 1e-3
        except IOError as e:
            error_description = str(e)
            is_server_overloaded = ("status is 503" in error_description
                                    and "Unknown error, please check your request or try again later." in error_description)
            if is_server_overloaded:
                o_id = "UNKNOWN"
                transact_time = time.time()
            else:
                raise
        return o_id, transact_time
    
    async def _all_trade_updates_for_order(self, order: InFlightOrder) -> List[TradeUpdate]:
        trade_updates = []
        try:
            exchange_order_id = await order.get_exchange_order_id()
            trading_pair = await self.exchange_symbol_associated_to_pair(trading_pair=order.trading_pair)
            all_fills_response = await self._api_get(
                path_url=constants.ACCOUNT_TRADE_LIST_URL,
                params={
                    "symbol": trading_pair,
                    "orderId": exchange_order_id
                },
                is_auth_required=True)

            for trade in all_fills_response:
                # [{'clientId': None, 
                # 'fee': '0.006325', 
                # 'feeSymbol': 'USDC', 
                # 'isMaker': False, 
                # 'orderId': '114268482883354626', 
                # 'price': '126.49', 
                # 'quantity': '0.1', 
                # 'side': 'Bid', 
                # 'symbol': 'SOL_USDC_PERP', 
                # 'systemOrderType': None, 
                # 'timestamp': '2025-04-02T12:57:56.809', 
                #  'tradeId': 4052628}]
                order_id = str(trade.get("orderId"))
                if order_id == exchange_order_id:
                    #position_side = trade["positionSide"]
                    position_action = PositionAction.NIL # todo не уверен но мы не знаем позицию
                                       
                    fee = TradeFeeBase.new_perpetual_fee(
                        fee_schema=self.trade_fee_schema(),
                        position_action=position_action,
                        percent_token=trade["feeSymbol"],
                        flat_fees=[TokenAmount(amount=Decimal(trade["fee"]), token=trade["feeSymbol"])]
                    )
                    dt = datetime.strptime(trade["timestamp"], '%Y-%m-%dT%H:%M:%S.%f')
                    trade_update: TradeUpdate = TradeUpdate(
                        trade_id=str(trade["tradeId"]),
                        client_order_id=order.client_order_id,
                        exchange_order_id=trade["orderId"],
                        trading_pair=order.trading_pair,
                        fill_timestamp=int(dt.timestamp()),
                        fill_price=Decimal(trade["price"]),
                        fill_base_amount=Decimal(trade["quantity"]),
                        fill_quote_amount=Decimal(str(trade["quantity"])) * Decimal(trade["price"]),
                        fee=fee,
                    )
                    trade_updates.append(trade_update)

        except asyncio.TimeoutError:
            raise IOError(f"Skipped order update with order fills for {order.client_order_id} "
                          "- waiting for exchange order id.")

        return trade_updates

    async def _request_order_status_from_history(self, tracked_order: InFlightOrder) -> OrderUpdate:
        trading_pair = await self.exchange_symbol_associated_to_pair(trading_pair=tracked_order.trading_pair)
        order_updates = await self._api_get(
            path_url=constants.ORDERS_HISTORY_PATH_URL,
            params={
                "symbol": trading_pair,
                "orderId": tracked_order.exchange_order_id
            },
            is_auth_required=True)

        if len(order_updates) < 1:
            raise IOError(f"Order {tracked_order.client_order_id} not found in order history")
        order_update = order_updates[0]

        status = constants.ORDER_STATE[order_update["status"]]
        # TODO maybe Cancelled => PartiallyFilled
        #if status == OrderState.OPEN and Decimal(order_update['executedQuantity']) > 0:
        #    status = OrderState.PARTIALLY_FILLED

        _order_update: OrderUpdate = OrderUpdate(
            trading_pair=tracked_order.trading_pair,
            update_timestamp=self.current_timestamp,
            new_state=status,
            client_order_id=tracked_order.client_order_id,
            exchange_order_id=order_update["id"],
        )
        return _order_update


    async def _request_order_status(self, tracked_order: InFlightOrder) -> OrderUpdate:
        try:
            trading_pair = await self.exchange_symbol_associated_to_pair(trading_pair=tracked_order.trading_pair)
            order_update = await self._api_get(
                path_url=constants.ORDERS_PATH_URL,
                params={
                    "symbol": trading_pair,
                    "clientId": int(tracked_order.client_order_id)
                },
                is_auth_required=True)
            if "code" in order_update:
                if self._is_request_exception_related_to_time_synchronizer(request_exception=order_update):
                    _order_update = OrderUpdate(
                        trading_pair=tracked_order.trading_pair,
                        update_timestamp=self.current_timestamp,
                        new_state=tracked_order.current_state,
                        client_order_id=tracked_order.client_order_id,
                    )
                    return _order_update
                elif self._is_order_not_found_during_cancelation_error(cancelation_exception=order_update):
                    return await self._request_order_status_from_history(tracked_order)
                else:
                    raise order_update

            status = constants.ORDER_STATE[order_update["status"]]
            if status == OrderState.OPEN and Decimal(order_update['executedQuantity']) > 0:
                status = OrderState.PARTIALLY_FILLED


            _order_update: OrderUpdate = OrderUpdate(
                trading_pair=tracked_order.trading_pair,

                update_timestamp=int(time.time())-1,
                new_state=status,
                client_order_id=order_update["clientId"],
                exchange_order_id=order_update["id"],
            )
            return _order_update
        except OSError as ex:
            if "RESOURCE_NOT_FOUND" in str(ex):
                return await self._request_order_status_from_history(tracked_order)
            else:
                raise ex

    
    async def _iter_user_event_queue(self) -> AsyncIterable[Dict[str, any]]:
        while True:
            try:
                yield await self._user_stream_tracker.user_stream.get()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger().network(
                    "Unknown error. Retrying after 1 seconds.",
                    exc_info=True,
                    app_warning_msg="Could not fetch user events from Backpack Perpetuals. Check API key and network connection.",
                )
                await self._sleep(1.0)
    
    async def _user_stream_event_listener(self):
        """
        Wait for new messages from _user_stream_tracker.user_stream queue and processes them according to their
        message channels. The respective UserStreamDataSource queues these messages.
        """
        async for event_message in self._iter_user_event_queue():
            try:
                await self._process_user_stream_event(event_message)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger().error(f"Unexpected error in user stream listener loop: {e}", exc_info=True)
                await self._sleep(5.0)
    
    async def _process_order_related_stream_event(self, event_message: Dict[str, Any]):
        event_type = event_message.get("e")
        client_order_id = str(event_message.get("c"))
        tracked_order = self._order_tracker.all_fillable_orders.get(client_order_id)
        if tracked_order is not None:
            trade_id: str = str(event_message["t"])

            # TODO not sure sbout 0
            if trade_id != "0" and event_type == "orderFill":  # Indicates that there has been a trade
                fee_asset = event_message.get("N", tracked_order.quote_asset)
                fee_amount = Decimal(event_message.get("n", "0"))
                position_action = PositionAction.NIL # TODO not sure about this
                flat_fees = [] if fee_amount == Decimal("0") else [TokenAmount(amount=fee_amount, token=fee_asset)]

                fee = TradeFeeBase.new_perpetual_fee(
                    fee_schema=self.trade_fee_schema(),
                    position_action=position_action,
                    percent_token=fee_asset,
                    flat_fees=flat_fees,
                )

                trade_update: TradeUpdate = TradeUpdate(
                    trade_id=trade_id,
                    client_order_id=client_order_id,
                    exchange_order_id=str(event_message["i"]),
                    trading_pair=tracked_order.trading_pair,
                    fill_timestamp=event_message["E"] * 1e-6,
                    fill_price=Decimal(event_message["L"]),
                    fill_base_amount=Decimal(event_message["l"]),
                    fill_quote_amount=Decimal(event_message["Z"]),
                    fee=fee,
                )
                self._order_tracker.process_trade_update(trade_update)


        tracked_order = self._order_tracker.all_updatable_orders.get(client_order_id)
        if tracked_order is not None:
            order_update: OrderUpdate = OrderUpdate(
                trading_pair=tracked_order.trading_pair,
                update_timestamp=event_message["E"] * 1e-6,
                new_state=constants.ORDER_STATE[event_message["X"]],
                client_order_id=client_order_id,
                exchange_order_id=str(event_message["i"]),
            )
            self._order_tracker.process_order_update(order_update)

    # {
    #   "e": "positionOpened",  // Event type
    #   "E": 1694687692980000,  // Event time in microseconds
    #   "s": "SOL_USDC_PERP",    // Symbol
    #   "b": 123,               // Break event price
    #   "B": 122,               // Entry price
    #   "l": 50,                // Estimated liquidation price
    #   "f": 0.5,               // Initial margin fraction
    #   "M": 122,               // Mark price
    #   "m": 0.01,              // Maintenance margin fraction
    #   "q": 5,                 // Net quantity
    #   "Q": 6,                 // Net exposure quantity
    #   "n": 732 ,              // Net exposure notional
    #   "i": "1111343026172067" // Position ID
    #   "p": "-1",              // PnL realized
    #   "P": "0",               // PnL unrealized
    #   "T": 1694687692989999   // Engine timestamp in microseconds
    # }
    async def _process_position_related_stream_event(self, event_message: Dict[str, Any]):
        trading_pair = event_message.get("s")
        event_type = event_message.get("e")
        try:
            hb_trading_pair = await self.trading_pair_associated_to_exchange_symbol(trading_pair)
        except KeyError:
            # Ignore results for which their symbols is not tracked by the connector
            return
        

        # тут кажется только один режим
        side = PositionSide.BOTH
        position = self._perpetual_trading.get_position(hb_trading_pair, side)
        if position is not None:
            amount = Decimal(event_message["q"])
            if amount == Decimal("0") or event_type == "positionClosed":
                pos_key = self._perpetual_trading.position_key(hb_trading_pair, side)
                self._perpetual_trading.remove_position(pos_key)
            else:
                position.update_position(position_side=PositionSide.BOTH,
                                            unrealized_pnl=Decimal(event_message["P"]),
                                            entry_price=Decimal(event_message["B"]),
                                            amount=Decimal(event_message["q"]))
        else:
            await self._update_positions()
                
    async def _process_user_stream_event(self, event_message: Dict[str, Any]):

        # TODO handle margin calls somehow ?
        stream = event_message.get('stream')
        self.logger().info(f"Received event stream: {stream}. Event message: {event_message}")

        match stream:
            case "account.orderUpdate":
                await self._process_order_related_stream_event(event_message['data'])
            case "account.positionUpdate":
                await self._process_position_related_stream_event(event_message['data'])
            case _:
                self.logger().warning(f"Received unexpected event stream: {stream}")

    async def _format_trading_rules(self, rules: List[Dict[str, Any]]) -> List[TradingRule]:
        """
        Queries the necessary API endpoint and initialize the TradingRule object for each trading pair being traded.

        Parameters
        ----------
        rules:
            Trading rules dictionary response from the exchange

            Example: [{
                    "baseSymbol": "SOL",
                    "createdAt": "2025-01-21T06:34:54.691858",
                    "filters": {
                        "price": {
                            "borrowEntryFeeMaxMultiplier": null,
                            "borrowEntryFeeMinMultiplier": null,
                            "maxImpactMultiplier": "1.1",
                            "maxMultiplier": "1.1",
                            "maxPrice": "1000",
                            "meanMarkPriceBand": { "maxMultiplier": "1.1", "minMultiplier": "0.9"  },
                            "meanPremiumBand": {  "tolerancePct": "0.01" },
                            "minImpactMultiplier": "0.9",
                            "minMultiplier": "0.9",
                            "minPrice": "0.01",
                            "tickSize": "0.01"
                        },
                        "quantity": {
                            "maxQuantity": null,
                            "minQuantity": "0.01",
                            "stepSize": "0.01"
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
                    "quoteSymbol": "USDC",
                    "symbol": "SOL_USDC_PERP"
                }]        
        """
        return_val: list = []
        for rule in rules:
            try:
                if web_utils.is_exchange_information_valid(rule):
                    trading_pair = await self.trading_pair_associated_to_exchange_symbol(symbol=rule["symbol"])
                    filters = rule["filters"]


                    # public str trading_pair
                    # public object min_order_size                   # Calculated min base asset size based on last trade price
                    # public object max_order_size                   # Calculated max base asset size
                    # public object min_price_increment              # Min tick size difference accepted (e.g. 0.1)
                    # public object min_base_amount_increment        # Min step size of base asset amount (e.g. 0.01)
                    # public object min_quote_amount_increment       # Min step size of quote asset amount (e.g. 0.01)
                    # public object max_price_significant_digits     # Max # of significant digits in a price
                    # public object min_notional_size                # Notional value = price * quantity, min accepted (e.g. 3.001)
                    # public object min_order_value                  # Calculated min base asset value based on the minimum accepted trade value (e.g. 0.078LTC is ~50,000 Satoshis)
                    # public bint supports_limit_orders              # if limit order is allowed for this trading pair
                    # public bint supports_market_orders             # if market order is allowed for this trading pair
                    # public object buy_order_collateral_token       # Indicates the collateral token used for buy orders
                    # public object sell_order_collateral_token      # Indicates the collateral token used for sell orders

                    from hummingbot.connector.trading_rule import (s_decimal_min, s_decimal_0, s_decimal_max)

                    max_order_size = Decimal(filters.get("quantity").get("maxQuantity")) if filters.get("quantity").get("maxQuantity") else s_decimal_max
                    min_order_size = Decimal(filters.get("quantity").get("minQuantity")) if filters.get("quantity").get("minQuantity") else s_decimal_0
                    step_size = Decimal(filters.get("quantity").get("stepSize")) if filters.get("quantity").get("stepSize") else s_decimal_min
                    tick_size = Decimal(filters.get("price").get("tickSize")) if filters.get("price").get("tickSize") else s_decimal_min
                    collateral_token = rule["quoteSymbol"]

                    return_val.append(
                        TradingRule(
                            trading_pair,
                            min_order_size=min_order_size,
                            max_order_size=max_order_size,
                            min_price_increment=tick_size,
                            min_base_amount_increment=step_size,
                            buy_order_collateral_token=collateral_token,
                            sell_order_collateral_token=collateral_token,
                            supports_limit_orders=True,
                            supports_market_orders=True,
                        )
                    )
            except Exception as e:
                self.logger().error(
                    f"Error parsing the trading pair rule {rule}. Error: {e}. Skipping...", exc_info=True
                )
        return return_val        

    def _initialize_trading_pair_symbols_from_exchange_info(self, exchange_info: List[Dict[str, Any]]):
        mapping = bidict()
        for symbol_data in filter(web_utils.is_exchange_information_valid, exchange_info):
            exchange_symbol = symbol_data["symbol"]
            base = symbol_data["baseSymbol"]
            quote = symbol_data["quoteSymbol"]
            trading_pair = combine_to_hb_trading_pair(base, quote)
            if trading_pair in mapping.inverse:
                self._resolve_trading_pair_symbols_duplicate(mapping, exchange_symbol, base, quote)
            else:
                mapping[exchange_symbol] = trading_pair
        self._set_trading_pair_symbol_map(mapping)


    async def _get_last_traded_price(self, trading_pair: str) -> float:
        exchange_symbol = await self.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
        params = {"symbol": exchange_symbol, "interval": "1d"}
        response = await self._api_get(
            path_url=constants.TICKER_PATH_URL,
            params=params)
        price = float(response["lastPrice"])
        return price

    def _resolve_trading_pair_symbols_duplicate(self, mapping: bidict, new_exchange_symbol: str, base: str, quote: str):
        """Resolves name conflicts provoked by futures contracts.

        If the expected BASEQUOTE combination matches one of the exchange symbols, it is the one taken, otherwise,
        the trading pair is removed from the map and an error is logged.
        """
        expected_exchange_symbol = f"{base}{quote}"
        trading_pair = combine_to_hb_trading_pair(base, quote)
        current_exchange_symbol = mapping.inverse[trading_pair]
        if current_exchange_symbol == expected_exchange_symbol:
            pass
        elif new_exchange_symbol == expected_exchange_symbol:
            mapping.pop(current_exchange_symbol)
            mapping[new_exchange_symbol] = trading_pair
        else:
            self.logger().error(
                f"Could not resolve the exchange symbols {new_exchange_symbol} and {current_exchange_symbol}")
            mapping.pop(current_exchange_symbol)

    async def _update_balances(self):
        """
        Calls the REST API to update total and available balances.
        """
        local_asset_names = set(self._account_balances.keys())
        remote_asset_names = set()

        account_info = await self._api_get(path_url=constants.ACCOUNTS_PATH_URL,
                                           is_auth_required=True)
        """
        example:
        {
            "property1": {
                "available": "string",
                "locked": "string",
                "staked": "string"
            },
            "property2": {
                "available": "string",
                "locked": "string",
                "staked": "string"
            },
            ...
        }
        """

        logging.getLogger(__name__).info(f"account_info: {account_info}")

        for asset_name, balance_data in account_info.items():
            available_balance = Decimal(balance_data.get("available", "0"))
            wallet_balance = available_balance + Decimal(balance_data.get("locked", "0"))

            self._account_available_balances[asset_name] = available_balance
            self._account_balances[asset_name] = wallet_balance
            remote_asset_names.add(asset_name)

        asset_names_to_remove = local_asset_names.difference(remote_asset_names)
        for asset_name in asset_names_to_remove:
            del self._account_available_balances[asset_name]
            del self._account_balances[asset_name]

    async def _update_positions(self):
        positions = await self._api_get(path_url=constants.POSITIONS_PATH_URL,
                                        is_auth_required=True)
        """ 
        Example: 
        [
            {
                "breakEvenPrice": "string",
                "entryPrice": "string",
                "estLiquidationPrice": "string",
                "imf": "string",
                "imfFunction": { ... },
                "markPrice": "string",
                "mmf": "string",
                "mmfFunction": { ... },
                "netCost": "string",
                "netQuantity": "string",
                "netExposureQuantity": "string",
                "netExposureNotional": "string",
                "pnlRealized": "string",
                "pnlUnrealized": "string",
                "cumulativeFundingPayment": "string",
                "subaccountId": 0,
                "symbol": "string",
                "userId": 0,
                "positionId": "string",
                "cumulativeInterest": "string"
            }
        ]
        """
        for position in positions:
            trading_pair = position.get("symbol")
            try:
                hb_trading_pair = await self.trading_pair_associated_to_exchange_symbol(trading_pair)
            except KeyError:
                # Ignore results for which their symbols is not tracked by the connector
                continue
            position_side = PositionSide.BOTH
            unrealized_pnl = Decimal(position.get("pnlUnrealized"))
            entry_price = Decimal(position.get("entryPrice"))
            amount = Decimal(position.get("netQuantity"))
            pos_key = self._perpetual_trading.position_key(hb_trading_pair, position_side)
            if amount != 0:
                _position = Position(
                    trading_pair=await self.trading_pair_associated_to_exchange_symbol(trading_pair),
                    position_side=position_side,
                    unrealized_pnl=unrealized_pnl,
                    entry_price=entry_price,
                    amount=amount,
                    leverage=self._leverage
                )
                self._perpetual_trading.set_position(pos_key, _position)
            else:
                self._perpetual_trading.remove_position(pos_key)


    async def _update_order_fills_from_trades(self):
        last_tick = int(self._last_poll_timestamp / self.UPDATE_ORDER_STATUS_MIN_INTERVAL)
        current_tick = int(self.current_timestamp / self.UPDATE_ORDER_STATUS_MIN_INTERVAL)
        if current_tick > last_tick and len(self._order_tracker.active_orders) > 0:
            trading_pairs_to_order_map: Dict[str, Dict[str, Any]] = defaultdict(lambda: {})
            for order in self._order_tracker.active_orders.values():
                trading_pairs_to_order_map[order.trading_pair][order.exchange_order_id] = order
            trading_pairs = list(trading_pairs_to_order_map.keys())
            tasks = [
                self._api_get(
                    path_url=constants.ACCOUNT_TRADE_LIST_URL,
                    params={"symbol": await self.exchange_symbol_associated_to_pair(trading_pair=trading_pair)},
                    is_auth_required=True,
                )
                for trading_pair in trading_pairs
            ]
            self.logger().debug(f"Polling for order fills of {len(tasks)} trading_pairs.")
            results = await safe_gather(*tasks, return_exceptions=True)
            for trades, trading_pair in zip(results, trading_pairs):
                order_map = trading_pairs_to_order_map.get(trading_pair)
                if isinstance(trades, Exception):
                    self.logger().network(
                        f"Error fetching trades update for the order {trading_pair}: {trades}.",
                        app_warning_msg=f"Failed to fetch trade update for {trading_pair}."
                    )
                    continue

                """
                # [{'clientId': None, 
                # 'fee': '0.006325', 
                # 'feeSymbol': 'USDC', 
                # 'isMaker': False, 
                # 'orderId': '114268482883354626', 
                # 'price': '126.49', 
                # 'quantity': '0.1', 
                # 'side': 'Bid', 
                # 'symbol': 'SOL_USDC_PERP', 
                # 'systemOrderType': None, 
                # 'timestamp': '2025-04-02T12:57:56.809', 
                #  'tradeId': 4052628}]
                #"""
                for trade in trades:
                    order_id = str(trade.get("orderId"))
                    if order_id in order_map:
                        tracked_order: InFlightOrder = order_map.get(order_id)
                        position_action = PositionAction.NIL
                        fee = TradeFeeBase.new_perpetual_fee(
                            fee_schema=self.trade_fee_schema(),
                            position_action=position_action,
                            percent_token=trade["feeSymbol"],
                            flat_fees=[TokenAmount(amount=Decimal(trade["fee"]), token=trade["feeSymbol"])]
                        )
                        timestamp = datetime.strptime(trade["timestamp"], '%Y-%m-%dT%H:%M:%S.%f').timestamp()

                        trade_update: TradeUpdate = TradeUpdate(
                            trade_id=str(trade["tradeId"]),
                            client_order_id=tracked_order.client_order_id,
                            exchange_order_id=trade["orderId"],
                            trading_pair=tracked_order.trading_pair,
                            fill_timestamp=timestamp,
                            fill_price=Decimal(trade["price"]),
                            fill_base_amount=Decimal(trade["quantity"]),
                            fill_quote_amount=Decimal(trade["price"]) * Decimal(trade["quantity"]),
                            fee=fee,
                        )
                        self._order_tracker.process_trade_update(trade_update)

    # async def _update_order_status(self):
    #     """
    #     Calls the REST API to get order/trade updates for each in-flight order.
    #     """
    #     last_tick = int(self._last_poll_timestamp / self.UPDATE_ORDER_STATUS_MIN_INTERVAL)
    #     current_tick = int(self.current_timestamp / self.UPDATE_ORDER_STATUS_MIN_INTERVAL)
    #     if current_tick > last_tick and len(self._order_tracker.active_orders) > 0:
    #         tracked_orders = list(self._order_tracker.active_orders.values())
    #         tasks = [
    #             self._api_get(
    #                 path_url=constants.ORDERS_PATH_URL,
    #                 params={
    #                     "symbol": await self.exchange_symbol_associated_to_pair(trading_pair=order.trading_pair),
    #                     "origClientOrderId": order.client_order_id
    #                 },
    #                 is_auth_required=True,
    #                 return_err=True,
    #             )
    #             for order in tracked_orders
    #         ]
    #         self.logger().debug(f"Polling for order status updates of {len(tasks)} orders.")
    #         results = await safe_gather(*tasks, return_exceptions=True)
    #
    #         for order_update, tracked_order in zip(results, tracked_orders):
    #             client_order_id = tracked_order.client_order_id
    #             if client_order_id not in self._order_tracker.all_orders:
    #                 continue
    #             if isinstance(order_update, Exception) or "code" in order_update:
    #                 if not isinstance(order_update, Exception) and \
    #                         (order_update["code"] == -2013 or order_update["msg"] == "Order does not exist."):
    #                     await self._order_tracker.process_order_not_found(client_order_id)
    #                 else:
    #                     self.logger().network(
    #                         f"Error fetching status update for the order {client_order_id}: " f"{order_update}."
    #                     )
    #                 continue
    #
    #             new_order_update: OrderUpdate = OrderUpdate(
    #                 trading_pair=await self.trading_pair_associated_to_exchange_symbol(order_update['symbol']),
    #                 update_timestamp=order_update["updateTime"] * 1e-3,
    #                 new_state=CONSTANTS.ORDER_STATE[order_update["status"]],
    #                 client_order_id=order_update["clientOrderId"],
    #                 exchange_order_id=order_update["orderId"],
    #             )
    #
    #             self._order_tracker.process_order_update(new_order_update)

    async def _get_position_mode(self) -> Optional[PositionMode]:
        return self._position_mode

    async def _trading_pair_position_mode_set(self, mode: PositionMode, trading_pair: str) -> Tuple[bool, str]:
        # TODO: Currently there are no position mode settings in Backpack
        return True, ""
    
    async def _set_trading_pair_leverage(self, mode: PositionMode, trading_pair: str) -> Tuple[bool, str]:
        # NOTE: There is no setting to set leverage in Backpack
        msg = "ok"
        success = True
        return success, msg
    
    """
    [
        {
        "userId": 0,
        "subaccountId": 0,
        "symbol": "string",
        "quantity": "string",
        "intervalEndTimestamp": "string",
        "fundingRate": "string"
        }
    """
    async def _fetch_last_fee_payment(self, trading_pair: str) -> Tuple[int, Decimal, Decimal]:
        exchange_symbol = await self.exchange_symbol_associated_to_pair(trading_pair)
        payment_response = await self._api_get(
            path_url=constants.GET_INCOME_HISTORY_URL,
            params={
                "symbol": exchange_symbol
            },
            is_auth_required=True,
        )
        funding_info_response = await self._api_get(
            path_url=constants.FUNDING_RATE_PATH_URL,
            params={
                "symbol": exchange_symbol,
            },
        )
        sorted_payment_response = sorted(payment_response, key=lambda a: datetime.strptime(a.get('intervalEndTimestamp'), '%Y-%m-%dT%H:%M:%S'), reverse=True)
        if len(sorted_payment_response) < 1:
            timestamp, funding_rate, payment = 0, Decimal("-1"), Decimal("-1")
            return timestamp, funding_rate, payment
        funding_payment = sorted_payment_response[0]
        _payment = Decimal(funding_payment["quantity"])
        #   {
        #     "fundingRate": "-0.000020321",
        #     "intervalEndTimestamp": "2025-04-02T16:00:00",
        #     "symbol": "SOL_USDC_PERP",
        #     "quantity": '0.027806',
        #   },
        funding_rate = Decimal(funding_payment["fundingRate"])
        timestamp = datetime.strptime(funding_payment["intervalEndTimestamp"], '%Y-%m-%dT%H:%M:%S').timestamp()
        if _payment != Decimal("0"):
            payment = _payment
        else:
            timestamp, funding_rate, payment = 0, Decimal("-1"), Decimal("-1")
        return timestamp, funding_rate, payment
