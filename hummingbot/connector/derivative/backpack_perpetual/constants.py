from decimal import Decimal
from typing import Dict, Optional

from hummingbot.connector.exchange_base import ExchangeBase

DEFAULT_FEES = {
    "maker": Decimal("0.0002"),  # 0.02%
    "taker": Decimal("0.0004"),  # 0.04%
}

# Base URLs
REST_URL = "https://api.backpack.exchange"
WSS_URL = "wss://ws.backpack.exchange"

# API endpoints
PING_PATH_URL = "/api/v1/ping"
TIME_PATH_URL = "/api/v1/time"
MARKETS_PATH_URL = "/api/v1/markets"
MARKET_PATH_URL = "/api/v1/market"
ORDER_BOOK_PATH_URL = "/api/v1/orderbook"
TICKER_PATH_URL = "/api/v1/ticker"
TRADES_PATH_URL = "/api/v1/trades"
ACCOUNTS_PATH_URL = "/api/v1/accounts"
POSITIONS_PATH_URL = "/api/v1/positions"
ORDERS_PATH_URL = "/api/v1/orders"
CANCEL_ORDER_PATH_URL = "/api/v1/orders/{order_id}"

# WebSocket channels
DIFF_EVENT_TYPE = "orderbook"
TRADE_EVENT_TYPE = "trades"
TICKER_EVENT_TYPE = "ticker"
POSITION_EVENT_TYPE = "position"
BALANCE_EVENT_TYPE = "balance"

# Rate Limits
RATE_LIMITS = {
    "ping": 1,
    "time": 1,
    "orderbook": 1,
    "ticker": 1,
    "trades": 1,
    "accounts": 1,
    "positions": 1,
    "orders": 1,
    "cancel_order": 1,
}

# Order status mapping
ORDER_STATUS_MAPPING = {
    "open": "OPEN",
    "filled": "FILLED",
    "cancelled": "CANCELED",
    "rejected": "FAILED",
    "expired": "EXPIRED",
}

# Error codes mapping
ERROR_CODES_MAPPING = {
    "INVALID_ORDER": "Invalid order",
    "INSUFFICIENT_BALANCE": "Insufficient balance",
    "ORDER_NOT_FOUND": "Order not found",
    "INVALID_PRICE": "Invalid price",
    "INVALID_QUANTITY": "Invalid quantity",
    "RATE_LIMIT_EXCEEDED": "Rate limit exceeded",
    "POSITION_NOT_FOUND": "Position not found",
    "INVALID_LEVERAGE": "Invalid leverage",
    "MAX_LEVERAGE_EXCEEDED": "Maximum leverage exceeded",
}

# Trading rules
TRADING_RULES = {
    "min_order_size": Decimal("0.00001"),
    "min_price_increment": Decimal("0.00001"),
    "min_base_amount_increment": Decimal("0.00001"),
    "max_order_size": Decimal("1000"),
    "max_price": Decimal("1000000"),
    "min_price": Decimal("0.00001"),
    "max_leverage": Decimal("100"),
    "min_leverage": Decimal("1"),
    "max_position_size": Decimal("1000"),
    "min_position_size": Decimal("0.00001"),
    "max_open_orders": 100,
    "max_orders_per_pair": 50,
}

# Position modes
POSITION_MODE = {
    "ONE_WAY": "ONE_WAY",
    "HEDGE": "HEDGE",
}

# Order types
ORDER_TYPES = {
    "LIMIT": "LIMIT",
    "MARKET": "MARKET",
    "STOP": "STOP",
    "STOP_MARKET": "STOP_MARKET",
    "TAKE_PROFIT": "TAKE_PROFIT",
    "TAKE_PROFIT_MARKET": "TAKE_PROFIT_MARKET",
}

# Time in force
TIME_IN_FORCE = {
    "GTC": "GTC",  # Good Till Cancel
    "IOC": "IOC",  # Immediate or Cancel
    "FOK": "FOK",  # Fill or Kill
    "GTX": "GTX",  # Good Till Crossing
    "GTT": "GTT",  # Good Till Time
}

# Leverage and margin constants
LEVERAGE_MULTIPLIER = Decimal("1.0")  # Default leverage multiplier
MAINTENANCE_MARGIN_RATE = Decimal("0.004")  # Default maintenance margin rate (0.4%) 