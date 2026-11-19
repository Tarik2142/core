"""Select platform for SLZB devices: the update channel (API v3)."""

from typing import override

from pysmlight import Api3
from pysmlight.catalog import FwChannel

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import SmConfigEntry, SmDataUpdateCoordinator
from .entity import SmEntity

PARALLEL_UPDATES = 1

# the channels of the device's updates settings, in the order its web UI offers them
CHANNELS: dict[str, FwChannel] = {"release": "release", "dev": "dev", "any": "any"}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SmConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the update channel select of a device with API v3."""
    coordinator = entry.runtime_data.data

    if (
        isinstance(coordinator.client, Api3)
        and coordinator.data.info.sel_fw_channel is not None
    ):
        async_add_entities([SmUpdateChannelSelect(coordinator)])


class SmUpdateChannelSelect(SmEntity, SelectEntity):
    """The update channel chosen on the device: the firmware it offers."""

    coordinator: SmDataUpdateCoordinator
    _attr_translation_key = "update_channel"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = list(CHANNELS)

    def __init__(self, coordinator: SmDataUpdateCoordinator) -> None:
        """Initialize the select."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.unique_id}-update_channel"

    @property
    @override
    def current_option(self) -> str | None:
        """Return the channel chosen on the device."""
        channel = self.coordinator.data.info.sel_fw_channel
        return channel if channel in CHANNELS else None

    @override
    async def async_select_option(self, option: str) -> None:
        """Choose the update channel on the device."""
        channel = CHANNELS[option]
        await self.coordinator.async_execute_command(
            self.coordinator.client.set_fw_channel, channel
        )
        self.coordinator.update_fw_channel(channel)
