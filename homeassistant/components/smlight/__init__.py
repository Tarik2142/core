"""SMLIGHT SLZB device integration."""

from pysmlight import Api3
from pysmlight.exceptions import SmlightConnectionError

from homeassistant.const import CONF_HOST, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .bluetooth import async_setup_ble_scanner
from .client import async_create_client
from .const import DOMAIN
from .coordinator import (
    SmConfigEntry,
    SmDataUpdateCoordinator,
    SmFirmwareUpdateCoordinator,
    SmlightData,
)
from .services import async_setup_services

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.INFRARED,
    Platform.LIGHT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.UPDATE,
]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the SMLIGHT services."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: SmConfigEntry) -> None:
    """Set up SMLIGHT from a config entry."""
    try:
        client = await async_create_client(hass, entry.data[CONF_HOST])
    except SmlightConnectionError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="cannot_connect_device",
            translation_placeholders={"error": str(err)},
        ) from err

    data_coordinator = SmDataUpdateCoordinator(hass, entry, client)
    firmware_coordinator = SmFirmwareUpdateCoordinator(hass, entry, client)

    await data_coordinator.async_config_entry_first_refresh()
    await firmware_coordinator.async_config_entry_first_refresh()

    info = data_coordinator.data.info

    if isinstance(client, Api3):
        # API v3: the device pushes every change of a resource over its WebSocket;
        # the client signs out of the device when the entry unloads
        entry.async_on_unload(client.close)
        entry.async_create_background_task(
            hass, client.live.run(), "smlight-live-client"
        )
    elif info.legacy_api < 2:
        entry.async_create_background_task(
            hass, client.sse.client(), "smlight-sse-client"
        )

    if info.ble is not None and (
        unload_callback := await async_setup_ble_scanner(hass, entry, client, info)
    ):
        entry.async_on_unload(unload_callback)

    entry.runtime_data = SmlightData(
        data=data_coordinator,
        firmware=firmware_coordinator,
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    if info.legacy_api < 2:
        entry.async_create_background_task(
            hass, firmware_coordinator.async_refresh(), "smlight-firmware-catalog"
        )


async def async_unload_entry(hass: HomeAssistant, entry: SmConfigEntry) -> None:
    """Unload SMLIGHT config entry."""
    await hass.config_entries.async_unload_platforms_or_raise(entry, PLATFORMS)
