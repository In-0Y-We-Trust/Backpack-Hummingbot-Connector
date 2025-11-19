import base64
import json
from typing import Any, Dict

from cryptography.hazmat.primitives.asymmetric import ed25519

from hummingbot.connector.derivative.backpack_perpetual.constants import REST_URL
from hummingbot.connector.time_synchronizer import TimeSynchronizer
from hummingbot.core.web_assistant.auth import AuthBase
from hummingbot.core.web_assistant.connections.data_types import RESTMethod, RESTRequest, WSRequest


class BackpackPerpetualAuth(AuthBase):
    """
    Auth class required by Backpack Perpetual API
    """
    def __init__(self, api_key: str, api_secret: str, time_provider: TimeSynchronizer):
        self._api_key: str = api_key
        self._api_secret: str = api_secret
        self._time_provider: TimeSynchronizer = time_provider
        self._private_key_obj = ed25519.Ed25519PrivateKey.from_private_bytes(base64.b64decode(api_secret))
        self._window:int = 5000

    
    async def rest_authenticate(self, request: RESTRequest) -> RESTRequest:
        """
        Adds the server time and the signature to the request, required for authenticated interactions.
        It also adds the required parameter in the request header.
        :param request: The request to be configured for authenticated interaction
        """
        timestamp = int(self._time_provider.time() * 1e3)
        request_params = {}
        instruction = self._get_instruction_from_request(request)

        # If there are data/params in the request, extract them
        if request.data:
            if isinstance(request.data, str):
                try:
                    request_params = json.loads(request.data)
                except ValueError:
                    pass
            else:
                request_params = request.data
        elif request.params:
            request_params = request.params

        # Generate the signature
        signature = self._generate_signature(instruction, timestamp, request_params)

        headers = {}
        if request.headers is not None:
            headers.update(request.headers)
        
        # Add auth headers
        headers.update({
            "X-API-Key": self._api_key,
            "X-Signature": signature,
            "X-Timestamp": str(timestamp),
            "X-Window": str(self._window),
            "Content-Type": "application/json; charset=utf-8",
        })
        request.headers = headers

        return request

    def get_api_key(self) -> str:
        """
        Returns the API key
        :return: The API key
        """
        return self._api_key

    def _generate_signature(self, action: str, timestamp: int, params: Dict[str, Any] = None) -> str:
        """
        Generate the signature for authentication
        :param action: The API action/instruction
        :param timestamp: Current timestamp in milliseconds
        :param params: Request parameters
        :return: Base64 encoded signature
        """
        if params:
            params = params.copy()
            for key, value in list(params.items()):
                if isinstance(value, bool):
                    params[key] = str(value).lower()

            param_str = "&" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        else:
            param_str = ""

        sign_str = f"instruction={action}{param_str}&timestamp={timestamp}&window={self._window}"
        signature = base64.b64encode(self._private_key_obj.sign(sign_str.encode())).decode()
        return signature

    def _get_instruction_from_request(self, request: RESTRequest) -> str:
        """
        Extract the appropriate instruction based on the request path and method
        """
        # Remove API_ENDPOINT prefix if present
        url = request.url.replace(REST_URL, "")
        
        # Map URL patterns and methods to instructions
        if "/api/v1/capital" in url and request.method == RESTMethod.GET:
            return "balanceQuery"
        elif "/wapi/v1/capital/deposit/address" in url and request.method == RESTMethod.GET:
            return "depositAddressQuery"
        elif "/wapi/v1/capital/deposits" in url and request.method == RESTMethod.GET:
            return "depositQueryAll"
        elif "/wapi/v1/history/orders" in url and request.method == RESTMethod.GET:
            return "orderHistoryQueryAll"
        elif "/wapi/v1/history/fills" in url and request.method == RESTMethod.GET:
            return "fillHistoryQueryAll"
        elif "/api/v1/order" in url:
            if request.method == RESTMethod.GET:
                return "orderQuery"
            elif request.method == RESTMethod.POST:
                return "orderExecute"
            elif request.method == RESTMethod.DELETE:
                return "orderCancel"
        elif "/api/v1/orders" in url:
            if request.method == RESTMethod.GET:
                return "orderQueryAll"
            elif request.method == RESTMethod.DELETE:
                return "orderCancelAll"
        elif "/wapi/v1/capital/withdrawals" in url:
            if request.method == RESTMethod.POST:
                return "withdraw"
            elif request.method == RESTMethod.GET:
                return "withdrawalQueryAll"
        elif "/api/v1/position" in url:
            return "positionQuery"
        elif "/wapi/v1/history/funding" in url:
            return "fundingHistoryQueryAll"
        
        raise Exception(f"No instruction found for URL: {url} and method: {request.method}")
    

    def generate_ws_signature(self, timestamp: int, window: int) -> str:
        sign_str = f"instruction=subscribe&timestamp={timestamp}&window={window}"
        signature = base64.b64encode(self._private_key_obj.sign(sign_str.encode())).decode()
        return signature

    
    async def ws_authenticate(self, request: WSRequest) -> WSRequest:
        return request  # pass-through
 