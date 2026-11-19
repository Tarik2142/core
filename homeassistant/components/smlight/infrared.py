"""Infrared platform for SLZB-Ultima."""

import logging
from typing import Any, override

from pysmlight import Api3
from pysmlight.const import Events as SmEvents
from pysmlight.exceptions import SmlightError
from pysmlight.models import IRPayload

from homeassistant.components.infrared import (
    InfraredCommand,
    InfraredEmitterEntity,
    InfraredReceivedSignal,
    InfraredReceiverEntity,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .coordinator import SmConfigEntry, SmDataUpdateCoordinator
from .entity import SmEntity

PARALLEL_UPDATES = 1

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SmConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Initialize infrared for SLZB-Ultima device."""
    coordinator = entry.runtime_data.data

    if coordinator.data.info.has_peripherals:
        async_add_entities(
            [
                SmInfraredEntity(coordinator),
                SmInfraredReceiverEntity(coordinator),
            ]
        )


class SmInfraredEntity(SmEntity, InfraredEmitterEntity):
    """Representation of a SLZB-Ultima infrared emitter."""

    def __init__(self, coordinator: SmDataUpdateCoordinator) -> None:
        """Initialize the SLZB-Ultima infrared."""
        super().__init__(coordinator)
        # Legacy format, kept as migrating existing unique IDs is not worth the risk
        self._attr_unique_id = f"{coordinator.unique_id}-infrared-emitter"  # pylint: disable=home-assistant-entity-unique-id-redundant-platform

    @override
    async def async_send_command(self, command: InfraredCommand) -> None:
        """Send an IR command."""
        # pysmlight's IRPayload.from_raw_timings expects positive durations,
        # so strip the sign from the signed pulse/space timings.
        timings = [abs(t) for t in command.get_raw_timings()]

        freq = command.modulation

        try:
            await self.coordinator.async_execute_command(
                self.coordinator.client.actions.send_ir_code,
                IRPayload.from_raw_timings(timings, freq=freq),
            )
        except (SmlightError, ValueError) as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="send_ir_code_failed",
                translation_placeholders={"error": str(err)},
            ) from err


class SmInfraredReceiverEntity(SmEntity, InfraredReceiverEntity):
    """Representation of a SLZB-Ultima infrared receiver."""

    def __init__(self, coordinator: SmDataUpdateCoordinator) -> None:
        """Initialize the SLZB-Ultima infrared receiver."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.unique_id}-receiver"

    @override
    async def async_added_to_hass(self) -> None:
        """Register SSE callbacks when entity is added to hass."""
        await super().async_added_to_hass()
        client = self.coordinator.client
        if isinstance(client, Api3):
            self.async_on_remove(client.live.subscribe("ir", self._handle_ir_v3))
        else:
            self.async_on_remove(
                client.sse.register_callback(SmEvents.IR_CODE, self._handle_ir_code)
            )

    @callback
    def _handle_ir_v3(self, data: dict[str, Any], origin: str | None) -> None:
        """Handle a code of resource ir (API v3): raw timings in 50 us ticks."""
        last = data.get("last")
        if not isinstance(last, dict) or not last.get("raw"):
            return
        try:
            timings = IRPayload(code=last["raw"]).to_raw_timings()
        except ValueError as err:
            _LOGGER.warning("Received IR code %s not decoded: %s", last["raw"], err)
            return
        self._handle_ir_code(timings)

    @callback
    def _handle_ir_code(self, timings: list[int]) -> None:
        """Handle received IR code."""
        self._handle_received_signal(InfraredReceivedSignal(timings=timings))
