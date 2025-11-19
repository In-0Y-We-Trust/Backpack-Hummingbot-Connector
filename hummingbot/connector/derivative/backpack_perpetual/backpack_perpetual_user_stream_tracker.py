import json
import time
from typing import TYPE_CHECKING, List, Optional

from hummingbot.connector.derivative.backpack_perpetual.backpack_perpetual_auth import BackpackPerpetualAuth
from hummingbot.core.data_type.user_stream_tracker_data_source import UserStreamTrackerDataSource
from hummingbot.core.web_assistant.connections.data_types import WSJSONRequest, WSRequest, WSPlainTextRequest
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory
from hummingbot.core.web_assistant.ws_assistant import WSAssistant
from hummingbot.logger.logger import HummingbotLogger

if TYPE_CHECKING:
    from hummingbot.connector.derivative.backpack_perpetual.backpack_perpetual_derivative import (
        BackpackPerpetualDerivative,
    )

class BackpackPerpetualUserStreamTracker(UserStreamTrackerDataSource):
    HEARTBEAT_TIME_INTERVAL = 30.0
    _logger: Optional[HummingbotLogger] = None

    def __init__(
            self,
            auth: BackpackPerpetualAuth,
            connector: 'BackpackPerpetualDerivative',
            api_factory: WebAssistantsFactory
    ):

        super().__init__()
        self._api_factory = api_factory
        self._auth = auth
        self._ws_assistants: List[WSAssistant] = []
        self._connector = connector
        self._listen_for_user_stream_task = None

    @property
    def last_recv_time(self) -> float:
        if self._ws_assistant:
            return self._ws_assistant.last_recv_time
        return 0
    
    async def _get_ws_assistant(self) -> WSAssistant:
        if self._ws_assistant is None:
            self._ws_assistant = await self._api_factory.get_ws_assistant()
        return self._ws_assistant
    
    async def _connected_websocket_assistant(self) -> WSAssistant:
        """
        Creates an instance of WSAssistant connected to the exchange
        """
        ws: WSAssistant = await self._get_ws_assistant()
        url = "wss://ws.backpack.exchange"
        await ws.connect(ws_url=url, ping_timeout=self.HEARTBEAT_TIME_INTERVAL)
        return ws


    """
    Subscribing to a private stream requires a valid signature generated from an ED25519 keypair. For stream subscriptions, the signature should be of the form:

    instruction=subscribe&timestamp=1614550000000&window=5000
    Where the timestamp and window are in milliseconds.
    For all markets: account.orderUpdate, account.positionUpdate
    """
    async def _subscribe_channels(self, websocket_assistant: WSAssistant):
        timestamp = int(time.time() * 1000)
        window = 5000
        
        # Generate signature first
        signature = self._auth.generate_ws_signature(timestamp, window)
        
        # Create request payload without the private key
        params = {
            "method": "SUBSCRIBE",
            "params": ["account.orderUpdate", "account.positionUpdate"],
            "signature": [self._auth.get_api_key(), signature, str(timestamp), str(window)]
        }
        
        subscribe_request = WSPlainTextRequest(json.dumps(params))
        await websocket_assistant.send(subscribe_request)
        self.logger().info("Subscribed to private account, position and orders channels...")
        

    async def _on_user_stream_interruption(self, websocket_assistant: Optional[WSAssistant]):
        websocket_assistant and await websocket_assistant.disconnect()
        await self._sleep(5)
