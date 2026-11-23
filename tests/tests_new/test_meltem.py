"""Tests for Meltem M-WRG support (meltem.py and the HVAC climate entity).

The expected payloads are the W 22F4 frames verified on a real
M-WRG-S M unit (32:150093, Nov 2026).
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.components.climate.const import HVACMode
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.ramses_cc.climate import RamsesHvac
from custom_components.ramses_cc.meltem import (
    MELTEM_FAN_MODES,
    airflow_code,
    airflow_payload,
    fan_speed_airflow,
    is_meltem,
    meltem_fan_mode,
)
from custom_components.ramses_cc.schemas import SCH_SET_MELTEM_AIRFLOW
from ramses_rf.devices import HvacVentilator
from ramses_rf.strategies.orcon import OrconStrategy

FAN_ID = "32:150093"
REM_ID = "37:150093"


@pytest.mark.parametrize(
    ("fan_mode", "payload"),
    [
        ("off", "00200000000000000000200000"),
        ("low", "0060E400000000000000200000"),
        ("medium", "0060E500000000000000200000"),
        ("high", "0060E600000000000000200000"),
        ("boost", "000000000060E3000000200000"),
        ("supply_only", "0080CDC9000000000000200000"),  # Zuluft 50 / 10
        ("extract_only", "0080C9CD000000000000200000"),  # Abluft 10 / 50
    ],
)
def test_fan_mode_payloads_match_verified_frames(
    fan_mode: str, payload: str
) -> None:
    # Arrange / Act / Assert
    assert MELTEM_FAN_MODES[fan_mode] == payload


@pytest.mark.parametrize(
    ("supply", "extract", "payload"),
    [
        (10, 50, "0080C9CD000000000000200000"),  # extract-only (Abluft)
        (50, 10, "0080CDC9000000000000200000"),  # supply-only (Zuluft)
        (40, 40, "0060CC00000000000000200000"),  # free balanced level
        (40, None, "0060CC00000000000000200000"),  # extract defaults
        (0, 0, "00800000000000000000200000"),  # fans off, flaps open
    ],
)
def test_airflow_payload_matches_verified_frames(
    supply: int, extract: int | None, payload: str
) -> None:
    # Arrange / Act / Assert
    assert airflow_payload(supply, extract) == payload


@pytest.mark.parametrize("airflow", [-10, 5, 15, 110])
def test_airflow_code_rejects_invalid_values(airflow: int) -> None:
    # Arrange / Act / Assert
    with pytest.raises(ValueError, match="Airflow must be"):
        airflow_code(airflow)


def test_airflow_code_range() -> None:
    # Arrange / Act / Assert
    assert airflow_code(0) == 0
    assert airflow_code(10) == 0xC9
    assert airflow_code(100) == 0xD2


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("VMD-22RES22", True),
        ("vmd-22res22 ", True),
        ("VMD-15RMS86", False),
        (None, False),
        (MagicMock(), False),
    ],
)
def test_is_meltem(model: Any, expected: bool) -> None:
    # Arrange
    device = MagicMock()
    device.model = model

    # Act / Assert
    assert is_meltem(device) is expected


@pytest.mark.parametrize(
    ("fan_info", "expected"),
    [
        ("off", "off"),
        ("speed 10 temporary override", "boost"),
        ("speed 3, high", "speed 3"),  # level 3 = 30 m³/h, not "high"
        ("speed 6", "speed 6"),
        (None, None),
    ],
)
def test_meltem_fan_mode(fan_info: str | None, expected: str | None) -> None:
    # Arrange / Act / Assert
    assert meltem_fan_mode(fan_info) == expected


@pytest.mark.parametrize(
    ("supply", "exhaust", "expected"),
    [
        (0.5, 0.1, "supply_only"),  # 31DA supply 64 / exhaust 14 (÷ 200)
        (0.1, 0.5, "extract_only"),  # 31DA supply 14 / exhaust 64
        (0.1, 0.1, "speed 5"),  # balanced: not named
        (0.6, 0.2, "speed 5"),  # other unbalanced airflows: not named
        (None, 0.5, "speed 5"),
        (True, 0.5, "speed 5"),
    ],
)
def test_meltem_fan_mode_from_fan_speeds(
    supply: Any, exhaust: Any, expected: str
) -> None:
    # Arrange / Act / Assert
    assert meltem_fan_mode("speed 5, medium", supply, exhaust) == expected


def test_meltem_fan_mode_off_and_boost_ignore_fan_speeds() -> None:
    # Arrange / Act / Assert
    assert meltem_fan_mode("off", 0.5, 0.1) == "off"
    assert meltem_fan_mode("speed 10 temporary override", 0.5, 0.1) == "boost"


def test_fan_speed_airflow() -> None:
    # Arrange / Act / Assert
    assert fan_speed_airflow(0.5) == 50
    assert fan_speed_airflow(0.1) == 10
    assert fan_speed_airflow(1) == 100
    assert fan_speed_airflow(None) is None
    assert fan_speed_airflow("0.5") is None


def test_service_schema_validates_airflow() -> None:
    # Arrange
    entity = {"entity_id": "climate.meltem"}

    # Act
    data = SCH_SET_MELTEM_AIRFLOW({**entity, "supply": "10", "extract": 50})

    # Assert
    assert data["supply"] == 10
    assert data["extract"] == 50
    with pytest.raises(Exception):  # noqa: B017 - vol.Invalid subclass
        SCH_SET_MELTEM_AIRFLOW({**entity, "supply": 15})


def _meltem_hvac(
    model: str | None = "VMD-22RES22",
    bound_rem: str | None = REM_ID,
    rem_faked: bool = True,
) -> tuple[RamsesHvac, MagicMock, MagicMock]:
    """Return a RamsesHvac for a mock Meltem FAN plus its gateway."""
    device = MagicMock(spec=HvacVentilator)
    device.id = FAN_ID
    device.model = model
    device.get_bound_rem = MagicMock(return_value=bound_rem)
    device.get_configured_strategy = MagicMock(return_value=OrconStrategy())
    device.set_fan_mode = AsyncMock()
    device.hvac_state = MagicMock(
        supply_fan_speed=None, exhaust_fan_speed=None
    )
    gateway = MagicMock()
    gateway.async_send_raw_command = AsyncMock()
    device.gateway = gateway

    rem = MagicMock()
    rem.is_faked = rem_faked
    coordinator = MagicMock()
    coordinator.remotes = {}
    coordinator.get_device = MagicMock(return_value=rem if bound_rem else None)

    description = MagicMock()
    description.key = "hvac"
    hvac = RamsesHvac(coordinator, device, description)
    hvac.async_write_ha_state = MagicMock()
    return hvac, device, gateway


def _sent_packet(gateway: MagicMock) -> str:
    """Return the packet string of the single raw command sent."""
    gateway.async_send_raw_command.assert_awaited_once()
    return str(gateway.async_send_raw_command.await_args.args[0])


def test_fan_modes_meltem_replaces_strategy_modes() -> None:
    # Arrange
    hvac, _, _ = _meltem_hvac()

    # Act
    modes = hvac.fan_modes

    # Assert: W 22F4 modes only, no Orcon 22F1 modes such as "away"
    assert modes == [
        "off",
        "low",
        "medium",
        "high",
        "boost",
        "supply_only",
        "extract_only",
    ]


def test_fan_modes_non_meltem_unchanged() -> None:
    # Arrange
    hvac, _, _ = _meltem_hvac(model="VMD-15RMS86")
    hvac.hass = MagicMock()
    hvac.hass.config.language = "en"

    # Act
    modes = hvac.fan_modes

    # Assert
    assert modes is not None
    assert "away" in modes
    assert "auto" in modes


async def test_set_fan_mode_meltem_sends_w_22f4_from_bound_rem() -> None:
    # Arrange
    hvac, device, gateway = _meltem_hvac()

    # Act
    await hvac.async_set_fan_mode("high")

    # Assert
    packet = _sent_packet(gateway)
    assert " W " in f" {packet} "
    assert REM_ID in packet
    assert FAN_ID in packet
    assert "22F4" in packet
    assert "0060E600000000000000200000" in packet
    device.set_fan_mode.assert_not_called()
    hvac.async_write_ha_state.assert_not_called()


async def test_set_fan_mode_meltem_extract_only() -> None:
    # Arrange
    hvac, _, gateway = _meltem_hvac()

    # Act
    await hvac.async_set_fan_mode("extract_only")

    # Assert: mode 4, supply 10 / extract 50 m³/h
    assert "0080C9CD000000000000200000" in _sent_packet(gateway)


async def test_set_fan_mode_meltem_without_bound_rem_raises() -> None:
    # Arrange
    hvac, _, gateway = _meltem_hvac(bound_rem=None)

    # Act / Assert
    with pytest.raises(HomeAssistantError, match="bound, faked remote"):
        await hvac.async_set_fan_mode("low")
    gateway.async_send_raw_command.assert_not_called()


async def test_set_fan_mode_meltem_rem_not_faked_raises() -> None:
    # Arrange
    hvac, _, gateway = _meltem_hvac(rem_faked=False)

    # Act / Assert
    with pytest.raises(HomeAssistantError, match="bound, faked remote"):
        await hvac.async_set_fan_mode("low")
    gateway.async_send_raw_command.assert_not_called()


async def test_set_fan_mode_meltem_user_command_takes_priority() -> None:
    # Arrange: a FAN _commands template overrides the built-in "low"
    hvac, _, gateway = _meltem_hvac()
    hvac.coordinator.remotes = {
        FAN_ID: {
            "low": {
                "verb": "W",
                "code": "22F4",
                "payload": "0060CC00000000000000200000",
            }
        }
    }

    # Act
    await hvac.async_set_fan_mode("low")

    # Assert
    assert "0060CC00000000000000200000" in _sent_packet(gateway)


async def test_set_hvac_mode_auto_meltem_sets_low() -> None:
    # Arrange
    hvac, _, gateway = _meltem_hvac()

    # Act
    await hvac.async_set_hvac_mode(HVACMode.AUTO)

    # Assert
    assert "0060E400000000000000200000" in _sent_packet(gateway)


async def test_set_hvac_mode_off_meltem_sets_off() -> None:
    # Arrange
    hvac, _, gateway = _meltem_hvac()

    # Act
    await hvac.async_set_hvac_mode(HVACMode.OFF)

    # Assert
    assert "00200000000000000000200000" in _sent_packet(gateway)


async def test_set_meltem_airflow_sends_unbalanced() -> None:
    # Arrange
    hvac, _, gateway = _meltem_hvac()

    # Act
    await hvac.async_set_meltem_airflow(supply=10, extract=50)

    # Assert
    assert "0080C9CD000000000000200000" in _sent_packet(gateway)


async def test_set_meltem_airflow_rejects_non_meltem() -> None:
    # Arrange
    hvac, _, gateway = _meltem_hvac(model="VMD-15RMS86")

    # Act / Assert
    with pytest.raises(ServiceValidationError, match="not a Meltem"):
        await hvac.async_set_meltem_airflow(supply=10)
    gateway.async_send_raw_command.assert_not_called()


async def test_set_meltem_airflow_rejects_invalid_airflow() -> None:
    # Arrange
    hvac, _, gateway = _meltem_hvac()

    # Act / Assert
    with pytest.raises(ServiceValidationError, match="Airflow must be"):
        await hvac.async_set_meltem_airflow(supply=15)
    gateway.async_send_raw_command.assert_not_called()


async def test_set_meltem_airflow_without_gateway_raises() -> None:
    # Arrange
    hvac, device, _ = _meltem_hvac()
    device.gateway = None

    # Act / Assert
    with pytest.raises(HomeAssistantError, match="Gateway unavailable"):
        await hvac.async_set_meltem_airflow(supply=10)


def test_fan_mode_meltem_uses_meltem_mapping() -> None:
    # Arrange
    hvac, _, _ = _meltem_hvac()
    hvac._get_effective_fan_info = MagicMock(  # type: ignore[method-assign]
        return_value="speed 3, high"
    )

    # Act / Assert: level 3 is not normalised to Orcon "high"
    assert hvac.fan_mode == "speed 3"


def test_fan_mode_meltem_extract_only_from_hvac_state() -> None:
    # Arrange: 31DA fan level 5, supply 10 / exhaust 50 m³/h
    hvac, device, _ = _meltem_hvac()
    hvac._get_effective_fan_info = MagicMock(  # type: ignore[method-assign]
        return_value="speed 5, medium"
    )
    device.hvac_state.supply_fan_speed = 0.1
    device.hvac_state.exhaust_fan_speed = 0.5

    # Act / Assert
    assert hvac.fan_mode == "extract_only"
