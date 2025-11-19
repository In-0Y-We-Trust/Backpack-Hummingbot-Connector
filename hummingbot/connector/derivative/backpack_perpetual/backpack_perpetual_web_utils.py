import time
from typing import Any, Callable, Dict, Optional

from hummingbot.connector.time_synchronizer import TimeSynchronizer
from hummingbot.connector.utils import TimeSynchronizerRESTPreProcessor
from hummingbot.core.api_throttler.async_throttler import AsyncThrottler
from hummingbot.core.web_assistant.auth import AuthBase
from hummingbot.core.web_assistant.connections.data_types import RESTMethod, RESTRequest
from hummingbot.core.web_assistant.rest_pre_processors import RESTPreProcessorBase
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory
from hummingbot.connector.derivative.backpack_perpetual import (
    backpack_perpetual_constants as constants,
)

from hummingbot.connector.derivative.backpack_perpetual.backpack_perpetual_constants import (
    PERPETUAL_BASE_URL,
    PERPETUAL_WS_URL)

# Mapping from URLs and endpoint paths to throttler limit IDs
THROTTLER_LIMIT_ID_MAP = {
    # Public endpoints
    "/api/v1/ping": "PUBLIC",
    "/api/v1/time": "PUBLIC",
    "/api/v1/markets": "PUBLIC",
    "/api/v1/market": "PUBLIC",
    "/api/v1/depth": "PUBLIC",
    "/api/v1/ticker": "PUBLIC",
    "/api/v1/trades": "PUBLIC",
    "/api/v1/premiumIndex": "PUBLIC",
    "/api/v1/fundingRates": "PUBLIC",
    "/api/v1/markPrices": "PUBLIC",
    
    # Private endpoints
    "/api/v1/order": "PRIVATE",
    "/api/v1/capital": "PRIVATE",
    "/api/v1/position": "PRIVATE",
    "/api/v1/leverage": "PRIVATE",
    "/api/v1/positionMode": "PRIVATE",
    "/api/v1/userDataStream": "PRIVATE",
    "/wapi/v1/history/fills": "PRIVATE",
    "/wapi/v1/history/funding": "PRIVATE",
    
    # Default rate limit for unknown endpoints
    "DEFAULT": "DEFAULT",
}


class BackpackPerpetualRESTPreProcessor(RESTPreProcessorBase):

    async def pre_process(self, request: RESTRequest) -> RESTRequest:
        if request.headers is None:
            request.headers = {}
        request.headers["Content-Type"] = (
            "application/json" if request.method == RESTMethod.POST else "application/x-www-form-urlencoded"
        )
        return request


def public_rest_url(path_url: str, domain: str = "backpack_perpetual"):
    return "https://api.backpack.exchange" + path_url


def private_rest_url(path_url: str, domain: str = "backpack_perpetual"):
    return "https://api.backpack.exchange" + path_url

def wss_url(endpoint: str, domain: str = "binance_perpetual"):
    return PERPETUAL_WS_URL + endpoint

def build_api_factory(
        throttler: Optional[AsyncThrottler] = None,
        time_synchronizer: Optional[TimeSynchronizer] = None,
        domain: str = constants.DEFAULT_DOMAIN,
        time_provider: Optional[Callable] = None,
        auth: Optional[AuthBase] = None) -> WebAssistantsFactory:
    throttler = throttler or create_throttler()
    time_synchronizer = time_synchronizer or TimeSynchronizer()
    time_provider = time_provider or (lambda: get_current_server_time(
        throttler=throttler,
        domain=domain,
    ))
    api_factory = WebAssistantsFactory(
        throttler=throttler,
        auth=auth,
        rest_pre_processors=[
            TimeSynchronizerRESTPreProcessor(synchronizer=time_synchronizer, time_provider=time_provider),
            BackpackPerpetualRESTPreProcessor(),
        ])
    return api_factory


def build_api_factory_without_time_synchronizer_pre_processor(throttler: AsyncThrottler) -> WebAssistantsFactory:
    api_factory = WebAssistantsFactory(
        throttler=throttler,
        rest_pre_processors=[BackpackPerpetualRESTPreProcessor()])
    return api_factory


def create_throttler() -> AsyncThrottler:
    from hummingbot.connector.derivative.backpack_perpetual.backpack_perpetual_constants import RATE_LIMITS
    return AsyncThrottler(RATE_LIMITS)


async def get_current_server_time(
        throttler: Optional[AsyncThrottler] = None,
        domain: str = constants.DEFAULT_DOMAIN
) -> float:
    """
    Fetches the current server time from Backpack Exchange.
    The API returns a simple text response with the server time in milliseconds.
    """
    import aiohttp
    
    async with aiohttp.ClientSession() as client:
        async with client.get("https://api.backpack.exchange/api/v1/time") as response:
            if response.status == 200:
                server_time_text = await response.text()
                return float(server_time_text.strip())
            else:
                # Fallback to local time if request fails
                return time.time() * 1000


def is_exchange_information_valid(rule: Dict[str, Any]) -> bool:
    """
    Verifies if a trading pair is enabled to operate with based on its exchange information

    :param rule: the exchange information for a trading pair

    :return: True if the trading pair is enabled, False otherwise
    """
    if rule["marketType"] == "PERP" and rule["orderBookState"] == "Open":
        valid = True
    else:
        valid = False
    return valid


# Add a function to get throttler limit ID from path URL
def get_throttler_limit_id(path_url: str) -> str:
    return THROTTLER_LIMIT_ID_MAP.get(path_url, THROTTLER_LIMIT_ID_MAP["DEFAULT"])
