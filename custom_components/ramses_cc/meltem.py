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

from typing import Any, Final

from homeassistant.components.climate.const import (
    FAN_HIGH,
    FAN_LOW,
    FAN_MEDIUM,
    FAN_OFF,
)

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
