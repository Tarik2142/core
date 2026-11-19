"""Tests of the SMLIGHT integration on SLZB-OS API v3 (pysmlight Api3)."""

import asyncio
from collections.abc import Callable, Generator
from datetime import timedelta
from ipaddress import ip_address
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from freezegun.api import FrozenDateTimeFactory
from pysmlight import Info, LiveClient, Sensors
from pysmlight.const import Pages, Settings
from pysmlight.exceptions import SmlightConnectionError
from pysmlight.models import SettingsEvent
from pysmlight.v3 import resources
from pysmlight.v3.client import ActionsV3
from pysmlight.web import CmdWrapper, Firmware
import pytest
from pytest_homeassistant.common import (
    MockConfigEntry,
    async_fire_time_changed,
    load_json_array_fixture,
    load_json_object_fixture,
)
from pytest_homeassistant.components.diagnostics import get_diagnostics_for_config_entry
from pytest_homeassistant.typing import ClientSessionGenerator

from homeassistant.components.infrared import async_subscribe_receiver
from homeassistant.components.smlight.const import DOMAIN
from homeassistant.components.update import (
    ATTR_IN_PROGRESS,
    ATTR_INSTALLED_VERSION,
    ATTR_UPDATE_PERCENTAGE,
    DOMAIN as UPDATE_DOMAIN,
    UpdateService,
)
from homeassistant.config_entries import SOURCE_USER, SOURCE_ZEROCONF, ConfigEntryState
from homeassistant.const import CONF_HOST, STATE_OFF, STATE_ON, STATE_UNKNOWN, TargetKey
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .conftest import MOCK_HOST, MOCK_ULTIMA, setup_integration

IR_RAW = "b4590c210c0a"  # 50 us ticks: 9000, 4450, 600, 1650, 600, 500


def firmware_list(*args: Any, **kwargs: Any) -> list[Firmware]:
    """The update server's lists, as in conftest."""
    if kwargs.get("mode") == "zigbee":
        name = (
            "zb_firmware.json"
            if kwargs.get("zb_type") == 0
            else "zb_firmware_router.json"
        )
    else:
        name = "esp_firmware.json"
    return [Firmware.from_dict(fw) for fw in load_json_array_fixture(name, DOMAIN)]


@pytest.fixture
def mock_v3_client() -> Generator[MagicMock]:
    """A device whose firmware answers API v3."""
    with (
        patch("homeassistant.components.smlight.client.Api3", autospec=True) as api3,
        patch(
            "homeassistant.components.smlight.client.detect_api_version",
            return_value=3,
        ) as detect,
    ):
        api = api3.return_value
        api.detect = detect
        api.host = MOCK_HOST
        api.get_info.return_value = Info.from_dict(
            load_json_object_fixture("info.json", DOMAIN)
        )
        sensors = Sensors.from_dict(load_json_object_fixture("sensors.json", DOMAIN))
        sensors.zb_temp = sensors.zb_temp2 = None  # API v3 has no radio temperature
        api.get_sensors.return_value = sensors
        api.get_firmware_version.side_effect = firmware_list
        api.check_auth_needed.return_value = False
        api.authenticate.return_value = True
        # instance attributes of Api3, not in its class
        api.live = MagicMock(spec_set=LiveClient)
        api.internet = AsyncMock(spec_set=resources.Internet)
        api.logs = AsyncMock(spec_set=resources.Logs)
        api.cmds = AsyncMock(spec_set=CmdWrapper)
        api.actions = AsyncMock(spec_set=ActionsV3)
        yield api


def subscribed(
    api: MagicMock, name: str
) -> Callable[[dict[str, Any], str | None], None]:
    """The callback the integration subscribed to a v3 resource with."""
    callbacks = [
        c.args[1] for c in api.live.subscribe.call_args_list if c.args[0] == name
    ]
    assert len(callbacks) == 1, f"{len(callbacks)} subscriptions to {name}"
    return callbacks[0]


async def test_setup_runs_the_live_link(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_v3_client: MagicMock,
) -> None:
    """API v3: the WebSocket link runs, the session ends with the entry."""
    entry = await setup_integration(hass, mock_config_entry)

    assert entry.state is ConfigEntryState.LOADED
    mock_v3_client.detect.assert_awaited_once()
    mock_v3_client.live.run.assert_called_once()
    mock_v3_client.authenticate.assert_awaited()  # the entry has credentials
    assert hass.states.get("sensor.mock_title_core_chip_temp").state == "35.0"
    # the radio's temperature is no value of API v3: no entity for it
    assert hass.states.get("sensor.mock_title_zigbee_chip_temp") is None

    await hass.config_entries.async_unload(entry.entry_id)
    mock_v3_client.close.assert_awaited_once()


async def test_setup_device_not_answering(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_v3_client: MagicMock,
) -> None:
    """No answer while the API is found: the setup is retried."""
    mock_v3_client.detect.side_effect = SmlightConnectionError("no route")
    entry = await setup_integration(hass, mock_config_entry)

    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_switch_settings_events(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_v3_client: MagicMock,
) -> None:
    """Switches follow the settings events of the WebSocket and set through Api3."""
    await setup_integration(hass, mock_config_entry)
    entity_id = "switch.mock_title_disable_leds"
    assert hass.states.get(entity_id).state == STATE_OFF

    callbacks = {
        c.args[0]: c.args[1]
        for c in mock_v3_client.live.register_settings_cb.call_args_list
    }
    assert set(callbacks) >= {Settings.DISABLE_LEDS, Settings.NIGHT_MODE}
    callbacks[Settings.DISABLE_LEDS](
        SettingsEvent(page=8, origin="mqtt", setting={"disableLeds": True})
    )
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == STATE_ON

    await hass.services.async_call(
        "switch", "turn_off", {TargetKey.ENTITY_ID: entity_id}, blocking=True
    )
    mock_v3_client.set_toggle.assert_awaited_with(
        Pages.API2_PAGE_SETTINGS_LED, "disableLeds", False
    )


async def test_internet_sensor(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_v3_client: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The internet sensor checks with resource internet and takes its pushes."""
    await setup_integration(hass, mock_config_entry)
    entity_id = "binary_sensor.mock_title_internet"
    assert hass.states.get(entity_id).state == STATE_UNKNOWN
    mock_v3_client.internet.check.assert_awaited_once()
    mock_v3_client.live.track.assert_any_call("internet")

    pushed = subscribed(mock_v3_client, "internet")
    pushed({"state": "checking"}, "device")
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == STATE_UNKNOWN  # no result yet
    pushed({"state": "ok", "ageS": 0}, "device")
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == STATE_ON
    pushed({"state": "none"}, "device")
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == STATE_OFF

    # fetched again after a reconnect
    (fetched,) = [c.args[0] for c in mock_v3_client.live.on_resync.call_args_list]
    mock_v3_client.live.state.return_value = {"state": "ok"}
    fetched()
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == STATE_ON
    mock_v3_client.live.state.assert_called_with("internet")

    freezer.tick(timedelta(minutes=15))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert mock_v3_client.internet.check.await_count == 2


async def test_ambilight_page_events(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_v3_client: MagicMock,
) -> None:
    """The Ambilight follows the page events of the WebSocket."""
    mock_v3_client.get_info.return_value = MOCK_ULTIMA
    await setup_integration(hass, mock_config_entry)

    (page, changed) = mock_v3_client.live.register_page_cb.call_args.args
    assert page == Pages.API2_PAGE_AMBILIGHT
    changed({"ultLedMode": 0, "ultLedBri": 80, "ultLedColor": 0xFF0000})
    await hass.async_block_till_done()

    state = hass.states.get("light.mock_title_ambilight")
    assert state.state == STATE_ON
    assert state.attributes["brightness"] == 80
    assert state.attributes["rgb_color"] == (255, 0, 0)


async def test_infrared_receiver(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_v3_client: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A code of resource ir reaches the receiver as timings."""
    mock_v3_client.get_info.return_value = MOCK_ULTIMA
    await setup_integration(hass, mock_config_entry)
    entity_id = "infrared.mock_title_infrared_receiver"
    signals = []
    async_subscribe_receiver(hass, entity_id, signals.append)

    pushed = subscribed(mock_v3_client, "ir")
    pushed({"last": None}, "device")  # forgotten: nothing received
    pushed({"last": {"raw": "zz"}}, "device")
    pushed(
        {"last": {"raw": IR_RAW, "proto": 8, "addr": 0, "cmd": 22, "seq": 4}}, "device"
    )
    await hass.async_block_till_done()

    assert [s.timings for s in signals] == [[9000, 4450, 600, 1650, 600, 500]]
    assert "not decoded" in caplog.text


@patch("homeassistant.components.smlight.update.asyncio.sleep", return_value=None)
async def test_core_update(
    mock_sleep: MagicMock,
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    mock_v3_client: MagicMock,
) -> None:
    """A core update follows resource ota until the device runs the new version."""
    await setup_integration(hass, mock_config_entry)
    entity_id = "update.mock_title_core_firmware"
    assert hass.states.get(entity_id).state == STATE_ON

    await hass.services.async_call(
        UPDATE_DOMAIN, UpdateService.INSTALL, {TargetKey.ENTITY_ID: entity_id}
    )
    mock_v3_client.fw_update.assert_awaited_once()
    ota = subscribed(mock_v3_client, "ota")

    ota({"state": "writing", "source": "url", "percent": 40}, "device")
    state = hass.states.get(entity_id)
    assert state.attributes[ATTR_IN_PROGRESS] is True
    assert state.attributes[ATTR_UPDATE_PERCENTAGE] == 40

    ota({"state": "done", "source": "url", "percent": 100}, "device")
    mock_v3_client.get_info.return_value = Info(sw_version="v2.7.5")
    freezer.tick(timedelta(seconds=5))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    state = hass.states.get(entity_id)
    assert state.state == STATE_OFF
    assert state.attributes[ATTR_INSTALLED_VERSION] == "v2.7.5"
    assert state.attributes[ATTR_IN_PROGRESS] is False


async def test_radio_update_failed(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_v3_client: MagicMock,
) -> None:
    """A radio's flash follows radioflash of its radio; an error fails the install."""
    await setup_integration(hass, mock_config_entry)
    entity_id = "update.mock_title_zigbee_firmware"

    install = asyncio.ensure_future(
        hass.services.async_call(
            UPDATE_DOMAIN,
            UpdateService.INSTALL,
            {TargetKey.ENTITY_ID: entity_id},
            blocking=True,
        )
    )
    async with asyncio.timeout(5):
        while not mock_v3_client.fw_update.await_count:
            await asyncio.sleep(0)
    flash = subscribed(mock_v3_client, "radioflash")

    flash({"state": "flashing", "idx": 1, "percent": 70}, "device")  # another radio
    assert hass.states.get(entity_id).attributes[ATTR_UPDATE_PERCENTAGE] is None
    flash({"state": "flashing", "idx": 0, "percent": 30}, "device")
    assert hass.states.get(entity_id).attributes[ATTR_UPDATE_PERCENTAGE] == 30
    flash({"state": "error", "idx": 0, "percent": 0, "error": "no answer"}, "device")

    with pytest.raises(HomeAssistantError):
        await install
    state = hass.states.get(entity_id)
    assert state.attributes[ATTR_IN_PROGRESS] is False
    assert state.attributes[ATTR_UPDATE_PERCENTAGE] is None


async def test_diagnostics_log(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    mock_config_entry: MockConfigEntry,
    mock_v3_client: MagicMock,
) -> None:
    """Diagnostics read the log with API v3."""
    mock_v3_client.logs.text.return_value = (
        "[19.11.2026] WEB3 | up\n[19.11.2026] ZB | ok"
    )
    entry = await setup_integration(hass, mock_config_entry)

    result = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    assert result["log"] == ["[19.11.2026] WEB3 | up", "[19.11.2026] ZB | ok"]
    assert result["info"]["model"] == "SLZB-06p7"


async def test_config_flow_user(
    hass: HomeAssistant, mock_setup_entry: AsyncMock, mock_v3_client: MagicMock
) -> None:
    """The user step finds API v3 and creates the entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: MOCK_HOST}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_HOST: MOCK_HOST}
    mock_v3_client.detect.assert_awaited_once()
    mock_v3_client.get_info.assert_awaited()


async def test_config_flow_zeroconf_asks_the_device_on_confirm(
    hass: HomeAssistant, mock_setup_entry: AsyncMock, mock_v3_client: MagicMock
) -> None:
    """A discovery with the MAC contacts the device only when the user confirms."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_ZEROCONF},
        data=ZeroconfServiceInfo(
            ip_address=ip_address(MOCK_HOST),
            ip_addresses=[ip_address(MOCK_HOST)],
            hostname="slzb-06.local.",
            name="mock_name",
            port=6638,
            properties={"mac": "AA:BB:CC:DD:EE:FF"},
            type="mock_type",
        ),
    )
    assert result["type"] is FlowResultType.FORM
    mock_v3_client.detect.assert_not_awaited()

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    mock_v3_client.detect.assert_awaited_once()
