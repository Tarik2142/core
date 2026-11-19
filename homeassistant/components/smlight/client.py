"""The client of a SLZB device: API v3 when its firmware answers it, else API v2."""

from pysmlight import Api2, Api3, detect_api_version

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

type SmClient = Api2 | Api3


async def async_create_client(hass: HomeAssistant, host: str) -> SmClient:
    """Return a client of the newest API the device answers.

    SLZB-OS v3 answers API v3 (REST + WebSocket); older firmware only API v2.
    Raises SmlightConnectionError when the device does not answer at all.
    """
    session = async_get_clientsession(hass)
    if await detect_api_version(host, session) == 3:
        return Api3(host, session=session)
    return Api2(host=host, session=session)
