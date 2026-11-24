"""Support for Meltem M-WRG ventilation units.

Meltem M-WRG units (e.g. M-WRG-S M) carry an Orcon RF module
(10E0 model ``VMD-22RES22``), so ramses_rf falls back to the Orcon
strategy for them.  That strategy's 22F1 mode commands are ignored by
the unit.  The unit is instead controlled with ``W 22F4``, carrying the
same mode block that the Meltem app and the M-WRG-GW gateway use:

====  ====================================================
Byte  Meaning
====  ====================================================
0     always ``00``
1     mode << 5 (1 off, 2 sensor, 3 balanced, 4 unbalanced)
2     supply (or balanced) level code
3     extract level code (unbalanced only)
5/6   intensive block (``60 E3``), with bytes 1-3 zero
10    always ``20``
====  ====================================================

Level codes are ``200 + m³/h ÷ 10`` (``C9`` = 10 … ``D2`` = 100 m³/h),
or the presets ``E4``/``E5``/``E6`` (LOW/MEDIUM/HIGH, the airflows of
Kennzahl 13/14/15) and ``E3`` (intensive, duration from Kennzahl 12).
Supply-only and extract-only ventilation are sent as mode 4 with the
factory airflows (50/10 and 10/50 m³/h).

The unit only accepts writes (``W 22F4``, ``W 2411``, ``W 10D0``) from
a bound remote: frames from the gateway's own ``18:`` address are
silently ignored.  Modes set this way are temporary: the unit's level
switch and a power cycle override them.

Verified on M-WRG-S M units, see ramses-rf/ramses_rf (Meltem decode
issue) and ``docs/source/usage.md``.
"""

from __future__ import annotations

import logging
from typing import Any, Final

from homeassistant.components.climate.const import (
    FAN_HIGH,
    FAN_LOW,
    FAN_MEDIUM,
    FAN_OFF,
)
from homeassistant.exceptions import HomeAssistantError

from ramses_rf import (
    SZ_DATA_TYPE,
    SZ_DATA_UNIT,
    SZ_DESCRIPTION,
    SZ_MAX_VALUE,
    SZ_MIN_VALUE,
    SZ_PRECISION,
)
from ramses_tx.const import Priority

from .helpers import (
    configured_hvac_strategy,
    device_gateway,
    parse_packet_string,
)

_LOGGER = logging.getLogger(__name__)

#: 10E0 model descriptions of Meltem M-WRG RF modules
MELTEM_MODELS: Final[frozenset[str]] = frozenset({"VMD-22RES22"})

#: Fan mode name for Meltem intensive ventilation ("Intensivlüftung")
FAN_BOOST: Final = "boost"
#: Fan mode name for supply-only ventilation ("Zuluftbetrieb")
FAN_SUPPLY_ONLY: Final = "supply_only"
#: Fan mode name for extract-only ventilation ("Abluftbetrieb")
FAN_EXTRACT_ONLY: Final = "extract_only"

#: Lowest and highest airflow accepted by the level codes, in m³/h
MIN_AIRFLOW: Final = 10
MAX_AIRFLOW: Final = 100
AIRFLOW_STEP: Final = 10

_MODE_OFF: Final = 1
_MODE_BALANCED: Final = 3
_MODE_UNBALANCED: Final = 4

_CODE_INTENSIVE: Final = 0xE3
_CODE_LOW: Final = 0xE4
_CODE_MEDIUM: Final = 0xE5
_CODE_HIGH: Final = 0xE6

#: Level codes of the factory supply/extract-only airflows (Kennzahl
#: 42/43 and 46/47): 10 and 50 m³/h.  No 22F4 preset code that uses
#: the unit's own Kennzahl values is known, so these are sent as
#: fixed airflows.
_CODE_10_M3H: Final = 0xC9
_CODE_50_M3H: Final = 0xCD

_PAYLOAD_LEN: Final = 13


def _payload(
    mode: int = 0,
    supply: int = 0,
    extract: int = 0,
    *,
    intensive: bool = False,
) -> str:
    """Return a 13-byte W 22F4 payload as an upper-case hex string."""
    buf = bytearray(_PAYLOAD_LEN)
    buf[10] = 0x20  # constant in every frame seen
    if intensive:
        buf[5], buf[6] = _MODE_BALANCED << 5, _CODE_INTENSIVE
    else:
        buf[1], buf[2], buf[3] = mode << 5, supply, extract
    return buf.hex().upper()


#: Fan mode name → W 22F4 payload
MELTEM_FAN_MODES: Final[dict[str, str]] = {
    FAN_OFF: _payload(_MODE_OFF),
    FAN_LOW: _payload(_MODE_BALANCED, _CODE_LOW),
    FAN_MEDIUM: _payload(_MODE_BALANCED, _CODE_MEDIUM),
    FAN_HIGH: _payload(_MODE_BALANCED, _CODE_HIGH),
    FAN_BOOST: _payload(intensive=True),
    FAN_SUPPLY_ONLY: _payload(_MODE_UNBALANCED, _CODE_50_M3H, _CODE_10_M3H),
    FAN_EXTRACT_ONLY: _payload(_MODE_UNBALANCED, _CODE_10_M3H, _CODE_50_M3H),
}


def is_meltem(device: Any) -> bool:
    """Return True if the device is a Meltem M-WRG unit.

    Detection uses the 10E0 model description, because ramses_rf has
    no Meltem scheme (``_scheme: meltem`` is rejected by its schema).
    Until the unit has answered a 10E0 request, this returns False.

    :param device: The ramses_rf device (e.g. an ``HvacVentilator``).
    :type device: Any
    :returns: True if the 10E0 model is a known Meltem RF module.
    :rtype: bool
    """
    model = getattr(device, "model", None)
    return isinstance(model, str) and model.strip().upper() in MELTEM_MODELS


def airflow_code(airflow: int) -> int:
    """Return the 22F4 level code for an airflow in m³/h.

    :param airflow: The airflow, 0 or 10-100 m³/h in steps of 10.
    :type airflow: int
    :returns: The level code (0, or 201-210).
    :rtype: int
    :raises ValueError: If the airflow is out of range or not a
        multiple of 10.
    """
    if airflow == 0:
        return 0
    if not MIN_AIRFLOW <= airflow <= MAX_AIRFLOW or airflow % AIRFLOW_STEP:
        raise ValueError(
            f"Airflow must be 0 or {MIN_AIRFLOW}-{MAX_AIRFLOW} m³/h in "
            f"steps of {AIRFLOW_STEP}: {airflow}"
        )
    return 200 + airflow // AIRFLOW_STEP


def airflow_payload(supply: int, extract: int | None = None) -> str:
    """Return the W 22F4 payload for a supply/extract airflow.

    Equal airflows give a balanced level (mode 3).  Different airflows
    give an unbalanced program (mode 4), e.g. extract-only ventilation
    ("Abluftbetrieb") with supply 10 and extract 50 m³/h.  Both at 0
    stops the fans with the air flaps open (unlike ``off``, which
    closes them).

    :param supply: The supply airflow in m³/h.
    :type supply: int
    :param extract: The extract airflow in m³/h (default: same as
        supply).
    :type extract: int | None
    :returns: The W 22F4 payload hex string.
    :rtype: str
    :raises ValueError: If an airflow is out of range.
    """
    if extract is None:
        extract = supply
    supply_code = airflow_code(supply)
    extract_code = airflow_code(extract)
    if supply == extract and supply:
        return _payload(_MODE_BALANCED, supply_code)
    return _payload(_MODE_UNBALANCED, supply_code, extract_code)


#: ramses_rf's 22F4 fan_mode for mode byte 0x20 (Meltem mode 1, "off"):
#: "paused" with the generic decode, "off" with ramses_rf's Meltem strategy
_22F4_MODES_OFF: Final = frozenset({"paused", "off"})

#: ramses_rf scheme name of the Meltem strategy (ramses_rf > 0.61.4)
_RF_MELTEM_SCHEME: Final = "meltem"


def meltem_is_off(fan_mode_22f4: Any) -> bool:
    """Return True if the unit's 22F4 status says it is off.

    In off mode the unit's 31DA reports fan_info ``00`` ("off"), but
    ramses_rf (up to 0.61.4) never lets an "off" fan_info replace an
    earlier value (a quirk for FANs that send it as a placeholder), so
    the 31DA state stays at the last level.  The unit's own 22F4 echo,
    sent after every change and every 10 minutes, carries the mode:
    ramses_rf decodes mode byte ``0x20`` (mode 1, off) as fan_mode
    ``"paused"``, or ``"off"`` with its Meltem strategy.

    :param fan_mode_22f4: The FAN's ``hvac_state.fan_mode``.
    :type fan_mode_22f4: Any
    :returns: True if the unit reports mode 1 (off).
    :rtype: bool
    """
    return fan_mode_22f4 in _22F4_MODES_OFF


def rf_decodes_meltem(device: Any) -> bool:
    """Return True if ramses_rf decodes this FAN with its Meltem strategy.

    ramses_rf releases after 0.61.4 have a Meltem strategy, selected by
    the 10E0 model: it decodes the 22F4 mode and airflow, the 10D0 filter
    days and the Meltem 2411 parameter set.  With older releases the unit
    is decoded as an Orcon FAN.

    :param device: The ramses_rf device (e.g. an ``HvacVentilator``).
    :type device: Any
    :returns: True if the configured strategy is ramses_rf's Meltem one.
    :rtype: bool
    """
    strategy = configured_hvac_strategy(device)
    return getattr(strategy, "scheme", None) == _RF_MELTEM_SCHEME


def meltem_param_schema(device: Any) -> dict[str, Any] | None:
    """Return the Meltem 2411 parameter set for a FAN, if ramses_rf has it.

    For a Meltem unit decoded by ramses_rf's Meltem strategy, that's the
    strategy's set (the Meltem Kennzahlen with their names and ranges).
    Otherwise None: callers use ramses_rf's generic set, as before.

    :param device: The ramses_rf device (e.g. an ``HvacVentilator``).
    :type device: Any
    :returns: Parameter ID → dict with description, min/max value,
        precision, data type and unit, or None.
    :rtype: dict[str, Any] | None
    """
    strategy = configured_hvac_strategy(device)
    fan_params = getattr(strategy, "fan_params", None)
    if (
        getattr(strategy, "scheme", None) != _RF_MELTEM_SCHEME
        or not isinstance(fan_params, dict)
        or not fan_params
    ):
        return None
    return {
        param_id: {
            SZ_DESCRIPTION: info.description,
            SZ_MIN_VALUE: info.min_value,
            SZ_MAX_VALUE: info.max_value,
            SZ_PRECISION: info.precision,
            SZ_DATA_TYPE: info.data_type,
            SZ_DATA_UNIT: info.data_unit,
        }
        for param_id, info in fan_params.items()
    }


def fan_speed_airflow(speed: Any) -> int | None:
    """Return the airflow in m³/h for a 31DA fan speed.

    ramses_rf decodes the 31DA supply/exhaust fan speed as a fraction
    (byte ÷ 200).  A Meltem unit reports its airflow there with 100 %
    = 100 m³/h, so the airflow is the fraction × 100.

    :param speed: The fan speed fraction (0.0-1.0) from ramses_rf.
    :type speed: Any
    :returns: The airflow in m³/h, or None if unknown.
    :rtype: int | None
    """
    if isinstance(speed, bool) or not isinstance(speed, int | float):
        return None
    return round(speed * 100)


#: (supply, extract) airflow in m³/h → fan mode, for unbalanced modes
_UNBALANCED_FAN_MODES: Final[dict[tuple[int, int], str]] = {
    (50, 10): FAN_SUPPLY_ONLY,
    (10, 50): FAN_EXTRACT_ONLY,
}


def meltem_fan_mode(
    fan_info: str | None,
    supply_speed: Any = None,
    exhaust_speed: Any = None,
) -> str | None:
    """Map the 31DA state of a Meltem unit to a fan mode.

    ramses_rf decodes the Meltem 31DA fan_info byte as the airflow
    level (1-10 = 10-100 m³/h), with Orcon labels such as
    ``"speed 3, high"``.  On a Meltem, level 3 is 30 m³/h, which is
    the MEDIUM preset, so the label suffix must not be matched against
    the fan modes.  Only unambiguous states are mapped:

    - ``"off"`` → ``off``
    - ``"speed 10 temporary override"`` (intensive) → ``boost``
    - supply/exhaust fan speeds of 50/10 m³/h → ``supply_only``
    - supply/exhaust fan speeds of 10/50 m³/h → ``extract_only``

    Other values are returned without the misleading label suffix
    (e.g. ``"speed 3"``).  Balanced levels aren't named, because the
    LOW/MEDIUM/HIGH airflows come from configurable Kennzahl 13-15.

    :param fan_info: The descriptive fan_info from ramses_rf.
    :type fan_info: str | None
    :param supply_speed: The 31DA supply fan speed fraction.
    :type supply_speed: Any
    :param exhaust_speed: The 31DA exhaust fan speed fraction.
    :type exhaust_speed: Any
    :returns: The fan mode, or the cleaned fan_info.
    :rtype: str | None
    """
    if not fan_info:
        return fan_info
    if fan_info == FAN_OFF:
        return FAN_OFF
    if "temporary override" in fan_info:
        return FAN_BOOST
    airflows = (
        fan_speed_airflow(supply_speed),
        fan_speed_airflow(exhaust_speed),
    )
    if airflows in _UNBALANCED_FAN_MODES:
        return _UNBALANCED_FAN_MODES[airflows]
    return fan_info.split(", ", 1)[0]


async def async_send_meltem_22f4(
    coordinator: Any,
    device: Any,
    payload: str,
    bound_rem: Any = None,
) -> None:
    """Send ``W 22F4`` to a Meltem unit from its bound, faked remote.

    The unit ignores W 22F4 from the gateway's own ``18:`` address, so
    without a bound, faked remote the command would be lost silently.
    The frame is sent with 2 repeats (W 22F4 is idempotent, and a frame
    was occasionally lost on air).  There is no optimistic update: the
    unit confirms with 22F1/22F4/31DA.

    :param coordinator: The RamsesCoordinator.
    :type coordinator: Any
    :param device: The Meltem FAN (ramses_rf ``HvacVentilator``).
    :type device: Any
    :param payload: The W 22F4 payload hex string.
    :type payload: str
    :param bound_rem: The bound remote's ID (default: from the FAN).
    :type bound_rem: Any
    :raises HomeAssistantError: If there is no bound, faked remote, the
        packet can't be built, or the gateway is unavailable.
    """
    # deferred: remote.py imports this module
    from .remote import _build_packet_from_template

    bound_rem = bound_rem or device.get_bound_rem()
    rem_dev = coordinator.get_device(str(bound_rem)) if bound_rem else None
    if rem_dev is None or not rem_dev.is_faked:
        raise HomeAssistantError(
            f"Meltem unit {device.id} needs a bound, faked remote to "
            "accept commands (see add_faked_rem and bind_device)"
        )
    packet_str = _build_packet_from_template(
        {
            "verb": "W",
            "code": "22F4",
            "payload": payload,
            "src": str(bound_rem),
        },
        device,
        coordinator,
    )
    _LOGGER.info("Sending Meltem command: %s", packet_str)
    cmd = parse_packet_string(packet_str)
    if cmd is None:
        raise HomeAssistantError(f"Failed to parse packet: {packet_str}")
    gwy = device_gateway(device)
    if gwy is None:
        raise HomeAssistantError(f"Gateway unavailable for device {device.id}")
    await gwy.async_send_raw_command(
        cmd, num_repeats=2, priority=Priority.HIGH
    )


#: Entity description keys hidden by default on a Meltem M-WRG-S M:
#: 31DA fields it reports as "not available" (7FFF/EF), the Orcon 22F4
#: rate (decoded with the wrong map), the 10D0 days (not decodable, see
#: filter_remaining_percent), 31DA flags, and the raw fan_info/fan_mode
#: decodes (the climate entity shows the mode).  The -F/-FC variants have
#: humidity or CO2 sensors, so those entities may carry data there and
#: can be unhidden.
MELTEM_HIDDEN_KEYS: Final[frozenset[str]] = frozenset(
    {
        # sensors
        "air_quality",
        "bypass_mode",
        "co2_level",
        "exhaust_flow",
        "fan_info",
        "fan_mode",
        "fan_rate",
        "filter_remaining",
        "indoor_humidity",
        "indoor_temp",
        "outdoor_humidity",
        "outdoor_temp",
        "post_heat",
        "pre_heat",
        "supply_flow",
        "supply_temp",
        "temperature",
        # binary sensors
        "bypass_position",
        "has_fault",
        "filter_dirty",
        "frost_cycle",
    }
)

#: 2411 parameter entities (``param_<ID>``) are all hidden on a Meltem
#: unit: ramses_rf polls the Orcon parameter set and names it with Orcon
#: names, but the IDs are Meltem Kennzahlen with other meanings (e.g.
#: ``param_65`` "Supply temperature limit" is Kennzahl 101, the external
#: switch mode), so changing one would write a different setting.
_PARAM_KEY_PREFIX: Final = "param_"


#: Keys that carry real values when ramses_rf decodes the unit with its
#: Meltem strategy (mode and airflow from 22F4, filter days from 10D0); the
#: 2411 parameter entities then use the Meltem set with correct names.
_RF_DECODED_KEYS: Final[frozenset[str]] = frozenset(
    {"fan_mode", "fan_rate", "filter_remaining"}
)


def meltem_hides_key(key: str | None, rf_decodes: bool = False) -> bool:
    """Return True if an entity with this key is hidden on Meltem units.

    :param key: The entity description key.
    :type key: str | None
    :param rf_decodes: True if ramses_rf decodes the unit with its Meltem
        strategy (see :func:`rf_decodes_meltem`).
    :type rf_decodes: bool
    :returns: True for the keys in MELTEM_HIDDEN_KEYS and all 2411
        parameter entities; with ``rf_decodes``, only for the keys that
        still have no value.
    :rtype: bool
    """
    if not key:
        return False
    if rf_decodes:
        return key in MELTEM_HIDDEN_KEYS and key not in _RF_DECODED_KEYS
    return key in MELTEM_HIDDEN_KEYS or key.startswith(_PARAM_KEY_PREFIX)
