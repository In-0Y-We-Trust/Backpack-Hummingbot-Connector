import asyncio
import copy
import hashlib
import hmac
import json
import unittest
from typing import Awaitable
from urllib.parse import urlencode

from hummingbot.connector.derivative.backpack_perpetual.backpack_perpetual_auth import BackpackPerpetualAuth
from hummingbot.core.web_assistant.connections.data_types import RESTMethod, RESTRequest, WSJSONRequest


class BackpackPerpetualAuthUnitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.ev_loop = asyncio.get_event_loop()
        cls.api_key = "TEST_API_KEY"
        cls.secret_key = "8JBN5Io91Do1YN9UatBqhfDP1A4MAkZ3ZS2eM78WBA4="

    def setUp(self) -> None:
        super().setUp()
        self.emulated_time = 1640001112.223
        self.test_params = {
            "test_param": "test_input",
            "timestamp": int(self.emulated_time * 1e3),
        }
        self.auth = BackpackPerpetualAuth(
            api_key=self.api_key,
            api_secret=self.secret_key,
            time_provider=self)

    def _get_test_payload(self):
        return urlencode(dict(copy.deepcopy(self.test_params)))

    def _get_signature_from_test_payload(self):
        return hmac.new(
            bytes(self.auth._api_secret.encode("utf-8")), self._get_test_payload().encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def async_run_with_timeout(self, coroutine: Awaitable, timeout: float = 1):
        ret = self.ev_loop.run_until_complete(asyncio.wait_for(coroutine, timeout))
        return ret

    def time(self):
        # Implemented to emulate a TimeSynchronizer
        return self.emulated_time

    def test_generate_signature(self):
        payload = self._get_test_payload()
        signature = self.auth._generate_signature("query", 12344444, self.test_params)

        self.assertEqual(signature, "41hTEikbSmuhLj85F7d+ybHtm+rpuaAiO1p0kMY713ZL7ygoAiG4C7ee76nxeGJZQ+fjzAGbtWfIRviqSoNrCw==")

    def test_rest_authenticate_parameters_provided(self):
        request: RESTRequest = RESTRequest(
            method=RESTMethod.GET, url="/api/v1/order", params=copy.deepcopy(self.test_params), is_auth_required=True
        )

        signed_request: RESTRequest = self.async_run_with_timeout(self.auth.rest_authenticate(request))

        self.assertIn("X-API-Key", signed_request.headers)
        self.assertEqual(signed_request.headers["X-API-Key"], self.api_key)
        self.assertIn("X-Signature", signed_request.headers)
        self.assertEqual(signed_request.headers["X-Signature"], "TRxvSdk2aLOxd4EW6muDTipn2sFoG5HSce9th8mnhq3R1AywB0hBaTRqp1485dOU+ICGdCqkDPkH5RBmPdlWDQ==")

    def test_rest_authenticate_data_provided(self):
        request: RESTRequest = RESTRequest(
            method=RESTMethod.POST, url="/api/v1/order", data=json.dumps(self.test_params), is_auth_required=True
        )

        signed_request: RESTRequest = self.async_run_with_timeout(self.auth.rest_authenticate(request))

        self.assertIn("X-API-Key", signed_request.headers)
        self.assertEqual(signed_request.headers["X-API-Key"], self.api_key)
        self.assertIn("X-Signature", signed_request.headers)
        self.assertEqual(signed_request.headers["X-Signature"], "+noWTe3iC+ddx8FbfGepAigl0yFKPNIH3RRlCT5Ic7+p3FD5vRfOGCa7Jra5jhDTDnVcmWPihZp7qbQ2JEACAQ==")

    def test_ws_authenticate(self):
        request: WSJSONRequest = WSJSONRequest(
            payload={"TEST": "SOME_TEST_PAYLOAD"}, throttler_limit_id="TEST_LIMIT_ID", is_auth_required=True
        )

        signed_request: WSJSONRequest = self.async_run_with_timeout(self.auth.ws_authenticate(request))

        self.assertEqual(request, signed_request)
