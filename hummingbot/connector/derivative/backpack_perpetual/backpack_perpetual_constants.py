"""
API endpoints for Backpack Perpetual connector.
"""

from hummingbot.core.api_throttler.data_types import RateLimit

# REST API endpoints
from hummingbot.core.data_type.in_flight_order import OrderState

REST_URL = "https://api.backpack.exchange"
WSS_URL = "wss://ws.backpack.exchange"

DEFAULT_DOMAIN = "backpack_perpetual_main"
EXCHANGE_NAME = "backpack_perpetual"


# Market data endpoints
PING_PATH_URL = "/api/v1/ping"
STATUS_PATH_URL = "/api/v1/status"
SERVER_TIME_PATH_URL = "/api/v1/time"
MARKETS_PATH_URL = "/api/v1/markets"
MARKET_PATH_URL = "/api/v1/market"
ORDER_BOOK_PATH_URL = "/api/v1/depth"
SNAPSHOT_PATH_URL = ORDER_BOOK_PATH_URL  # Alias for ORDER_BOOK_PATH_URL
TICKER_PATH_URL = "/api/v1/ticker"
TRADES_PATH_URL = "/api/v1/trades"
FUNDING_RATE_PATH_URL = "/api/v1/fundingRates"

PERPETUAL_BASE_URL="https://api.backpack.exchange"
PERPETUAL_WS_URL="wss://ws.backpack.exchange"

MARK_PRICE_PATH_URL = "/api/v1/markPrices"

DIFF_STREAM_ID = 1
TRADE_STREAM_ID = 2
FUNDING_INFO_STREAM_ID = 3

# Trading endpoints
ORDERS_PATH_URL = "/api/v1/order"
ORDERS_HISTORY_PATH_URL = "/wapi/v1/history/orders"
CANCEL_ORDER_PATH_URL = "/api/v1/order"
ACCOUNTS_PATH_URL = "/api/v1/capital"
POSITIONS_PATH_URL = "/api/v1/position"
LEVERAGE_PATH_URL = "/api/v1/leverage"
USER_STREAM_PATH_URL = "/api/v1/userDataStream"
ACCOUNT_TRADE_LIST_URL = "/wapi/v1/history/fills"
GET_INCOME_HISTORY_URL = "/wapi/v1/history/funding"

# WebSocket channels
DIFF_EVENT_TYPE = "depth"
TRADE_EVENT_TYPE = "trade"
TICKER_EVENT_TYPE = "ticker"
MARK_PRICE_EVENT_TYPE = "markPrice"
FUNDING_RATE_EVENT_TYPE = "fundingRate" 

# Rate Limit time intervals
ONE_HOUR = 3600
ONE_MINUTE = 60
ONE_SECOND = 1
ONE_DAY = 86400

# Define rate limit constants
DEFAULT_RATE_LIMIT_ID = "DEFAULT"
PUBLIC_RATE_LIMIT_ID = "PUBLIC"
PRIVATE_RATE_LIMIT_ID = "PRIVATE"

RATE_LIMITS = [
    # Default rate limits with reasonable values
    RateLimit(
        limit_id=DEFAULT_RATE_LIMIT_ID,
        limit=100,
        time_interval=ONE_SECOND,
        weight=1
    ),
    # Public endpoints
    RateLimit(
        limit_id=PUBLIC_RATE_LIMIT_ID,
        limit=20,
        time_interval=ONE_SECOND,
        weight=1
    ),
    # Private endpoints
    RateLimit(
        limit_id=PRIVATE_RATE_LIMIT_ID,
        limit=10,
        time_interval=ONE_SECOND,
        weight=1
    )
]

MAX_ID_BIT_COUNT = 31
MAX_ORDER_ID_LEN = 9 # там uint32

ORDER_STATE = {
    "New": OrderState.OPEN,
    "Filled": OrderState.FILLED,
    "PartiallyFilled": OrderState.PARTIALLY_FILLED,
    "Cancelled": OrderState.CANCELED,
    "Expired": OrderState.CANCELED,
    "TriggerFailed": OrderState.OPEN, # TODO not sure
    "TriggerPending": OrderState.OPEN, # TODO not sure
}

HEARTBEAT_TIME_INTERVAL = 30.0



