"""Collect diagnostics for SMLIGHT devices."""

from typing import Any

from pysmlight import Api3
from pysmlight.const import Actions

from homeassistant.core import HomeAssistant

from .coordinator import SmConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, config_entry: SmConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordintator = config_entry.runtime_data.data
    info = await coordintator.client.get_info()
    client = coordintator.client
    if isinstance(client, Api3):
        log = await client.logs.text() or "none"
    else:
        log = await client.get({"action": Actions.API_GET_LOG.value}) or "none"

    return {
        "info": info.to_dict(),
        "log": log.split("\n"),
    }
