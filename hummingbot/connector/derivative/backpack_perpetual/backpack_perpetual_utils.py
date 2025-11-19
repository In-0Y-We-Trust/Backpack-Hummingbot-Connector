from decimal import Decimal

from pydantic import Field, SecretStr

from hummingbot.client.config.config_data_types import BaseConnectorConfigMap, ClientFieldData
from hummingbot.core.data_type.trade_fee import TradeFeeSchema

DEFAULT_FEES = TradeFeeSchema(
    maker_percent_fee_decimal=Decimal("0.0002"),
    taker_percent_fee_decimal=Decimal("0.0005"),
    # TODO not sure buy_percent_fee_deducted_from_returns=True
)

CENTRALIZED = True

EXAMPLE_PAIR = "SOL_USDC"


class BackpackPerpetualConfigMap(BaseConnectorConfigMap):
    connector: str = Field(
        default="backpack_perpetual",
        client_data=None,
    )
    backpack_api_key: SecretStr = Field(
        default=...,
        client_data=ClientFieldData(
            prompt=lambda cm: "Enter your Backpack API key",
            is_secure=True,
            is_connect_key=True,
            prompt_on_new=True,
        )
    )
    backpack_secret_key: SecretStr = Field(
        default=...,
        client_data=ClientFieldData(
            prompt=lambda cm: "Enter your Backpack secret key",
            is_secure=True,
            is_connect_key=True,
            prompt_on_new=True,
        )
    )

# Required constants for connector settings
KEYS = BackpackPerpetualConfigMap.construct()