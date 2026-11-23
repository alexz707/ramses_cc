# Meltem M-WRG ventilation units

Meltem M-WRG units (for example the M-WRG-S M) contain an Orcon RF module.
ramses_rf has no Meltem scheme, so these units fall back to the Orcon strategy.
The unit ignores that strategy's `22F1` mode commands. ramses_cc therefore
controls Meltem units with `W 22F4` instead, the same mode block that the Meltem
app and the M-WRG-GW gateway use.

Everything on this page was verified on M-WRG-S M units (no humidity or CO2
sensors). The -F/-FC variants are untested.

## Detection

A FAN is treated as a Meltem unit when its 10E0 model is `VMD-22RES22`. The
model is shown by the FAN's *Model* sensor. Until the unit has answered a 10E0
request, it is handled like any other Orcon FAN.

`_scheme: meltem` can't be used: ramses_rf's schema only accepts its own
schemes.

## Pairing a faked remote (required)

The unit only accepts writes (`W 22F4`, `W 2411`, `W 10D0`) from a **bound
remote**. Writes from the gateway's own `18:` address are silently ignored, so
without a bound remote, fan modes and parameter changes have no effect. Reads
(`RQ 2411`, `RQ 10D0`) work from the gateway.

The FAN's device page has two buttons for this: **Create remote** (step 1,
with the address `37:` plus the FAN's number, e.g. `37:149304` for
`32:149304`) and **Bind remote** (step 3, with the offer of the FAN's
strategy). Without the buttons, use the actions below.

1. Create a faked remote bound to the FAN. This only changes the schema and
   sends nothing on air:

   ```yaml
   action: ramses_cc.add_faked_rem
   data:
     device_id: "37:150093"    # any unused 37: address
     bound_to: "32:150093"     # the Meltem FAN
     alias: "Virtual REM Meltem"
   ```

2. Switch the unit off and on at the mains. For the first 5 minutes after
   power-on it accepts new remotes ("Verbindungsmodus").

3. Within that window, press **Bind remote**, or bind the faked remote with an
   Orcon remote offer:

   ```yaml
   action: ramses_cc.bind_device
   data:
     device_id: "37:150093"
     offer: '{"22F1": "00", "22F3": "00"}'
   ```

   The unit accepts with `W 1FC9 … 0031DA… 0031D9…`, and the log shows
   "Binding process completed". The remote now takes one of the unit's remote
   slots.

4. Check it with a harmless parameter read or a fan mode change (see below).
   The unit acknowledges a `W 2411` from the remote with `I 2411` within a
   fraction of a second.

An on-air unbind isn't verified for Meltem units. `remove_device` only removes
the remote from the schema; the unit keeps the slot.

## Fan modes

For a Meltem FAN, the climate entity offers these fan modes, all sent as
`W 22F4` from the bound, faked remote:

| Fan mode | Meltem name | Payload |
| :--- | :--- | :--- |
| `off` | Gerät AUS (fans stop, air flaps close) | `00200000000000000000200000` |
| `low` | Stufe I / LOW (airflow of Kennzahl 13) | `0060E400000000000000200000` |
| `medium` | Stufe II / MEDIUM (Kennzahl 14) | `0060E500000000000000200000` |
| `high` | Stufe III / HIGH (Kennzahl 15) | `0060E600000000000000200000` |
| `boost` | Intensivlüftung (Kennzahl 11 airflow for Kennzahl 12 minutes) | `000000000060E3000000200000` |
| `supply_only` | Zuluftbetrieb (supply 50 / extract 10 m³/h) | `0080CDC9000000000000200000` |
| `extract_only` | Abluftbetrieb (supply 10 / extract 50 m³/h) | `0080C9CD000000000000200000` |

`supply_only` and `extract_only` send fixed airflows: the factory values of
Kennzahl 42/43 and 46/47. No 22F4 code is known that makes the unit use its own
Kennzahl values, so changing those Kennzahl doesn't change these modes. For
other airflows, use `set_meltem_airflow` (below) or override the mode in the
FAN's `_commands`.

The Orcon modes (`away`, `auto`, …) are not offered, because the unit ignores
them.

The power buttons switch the unit off and on, and on selects `low`:

- the climate entity's *Turn off* / *Turn on* (HVAC mode *off* / *auto*)
- the FAN's remote entity on the device page (`remote.fan_…`). Its toggle
  follows the unit's state. For other FANs this button still does nothing.

Behaviour of the unit:

- **Modes are temporary.** Moving the unit's level switch overrides an RF mode,
  and after a power cycle the unit returns to the switch position.
- **Intensive** always runs for Kennzahl 12 minutes; the command has no
  duration field. The countdown uses the live Kennzahl 12 value, so changing it
  during a run moves the end time. When the timer ends, the unit returns to the
  previous level, not to the switch position. Any mode command ends intensive
  early.
- The unit confirms a command within about 100 ms with `22F1`, `22F4` and
  `31DA` broadcasts. A command was occasionally lost on air during testing, so
  ramses_cc sends each command with 2 repeats. A repeated `W 22F4` is harmless.

Commands in the FAN's own `_commands` take priority over these built-in modes,
so a mode can be overridden with a `{verb, code, payload}` template. Templates
are sent from the bound remote too.

The current fan mode comes from the unit's status:

- `off` comes from the unit's `22F4` status (mode 1, which ramses_rf decodes
  as `paused`). The `31DA` fan level also reports off, but ramses_rf never
  lets an "off" fan level replace an earlier value.
- `boost` comes from the `31DA` fan level.
- `supply_only` and `extract_only` come from the supply and exhaust fan
  speeds. On a Meltem unit, 100 % is 100 m³/h, so 50 % / 10 % is
  supply-only and 10 % / 50 % is extract-only. Other unbalanced airflows
  set with `set_meltem_airflow` aren't named.
- Balanced levels are shown as `speed n` (level *n* = *n* × 10 m³/h). They
  aren't named, because the LOW/MEDIUM/HIGH airflows are set by Kennzahl
  13–15. ramses_rf labels level 3 `"speed 3, high"`, but on a Meltem unit
  that's 30 m³/h, the MEDIUM preset.

## Hidden entities

The unit's status lacks many of the values ramses_cc shows for other FANs.
On a Meltem unit, ramses_cc hides these entities by default:

- sensors without a value: air quality, CO2, indoor/outdoor temperature and
  humidity, supply temperature, supply/exhaust flow, bypass mode, pre/post
  heat, the 10D0 filter days, and the Orcon-decoded fan rate
- binary sensors: bypass position, fault, filter dirty, frost cycle
- the raw `fan_info` and `fan_mode` sensors (the climate entity shows the
  mode)
- all 2411 parameter entities. ramses_rf names them after the Orcon
  parameter set, but on a Meltem unit the IDs are Kennzahlen with other
  meanings (`param_65` "Supply temperature limit" is Kennzahl 101, the
  external switch mode), so changing one would write a different setting.
  Use `set_fan_param` with the Kennzahl table below instead.

They're hidden once, after the unit's model is known. An entity you unhide
stays visible. The -F/-FC variants have humidity or CO2 sensors, so unhide
those entities if they show values there.

## Setting the airflow

`ramses_cc.set_meltem_airflow` sets any supply and extract airflow, like the
Meltem remote's manual mode. Airflows are 0, or 10 to 100 m³/h in steps of 10.

```yaml
action: ramses_cc.set_meltem_airflow
target:
  entity_id: climate.32_150093
data:
  supply: 10
  extract: 50     # optional, defaults to the supply value
```

| Supply / extract | Result | Payload |
| :--- | :--- | :--- |
| 40 / 40 (or extract omitted) | balanced 40 m³/h | `0060CC00000000000000200000` |
| 10 / 50 | extract-only ventilation (Abluftbetrieb) | `0080C9CD000000000000200000` |
| 50 / 10 | supply-only ventilation (Zuluftbetrieb) | `0080CDC9000000000000200000` |
| 0 / 0 | fans stop, air flaps stay **open** | `00800000000000000000200000` |

Like the fan modes, the setting is temporary. The service raises an error if the
FAN isn't a Meltem unit, or if it has no bound, faked remote.

## Parameters (2411)

A 2411 parameter ID is the Meltem "Kennzahl" (decimal) from the M-WRG-FBH
remote manual, written in hex. Changes are acknowledged with `I 2411` and stored
permanently. Writes need the bound remote (see above): `set_fan_param` picks the
FAN's bound remote as the sender.

Verified over RF:

| ID | Kennzahl | Meaning | Range, step |
| :--- | :--- | :--- | :--- |
| `07` | 7 | Pause between sniffing runs (min) | 1–255, 1 |
| `08` | 8 | Sniffing duration (min) | 5–255, 1 |
| `09` | 9 | Airflow while sniffing (m³/h) | 10–100, 10 |
| `0A` | 10 | Air flaps in standby (0 open, 1 closed) | 0–1, 1 |
| `0B` | 11 | Intensive airflow (m³/h) | 0–100, 10 |
| `0C` | 12 | Intensive duration (min) | 0–240, 1 |
| `0D` / `0E` / `0F` | 13 / 14 / 15 | LOW / MEDIUM / HIGH airflow (m³/h) | 0–100, 10 |
| `10` / `11` / `12` | 16 / 17 / 18 | Mode of switch position I / II / III | 0–14, 1 |
| `24` | 36 | Humidity control start value (%) | 40–80, 1 |
| `2A` / `2B` | 42 / 43 | Extract / supply in supply-only mode (m³/h) | 0–100, 10 |
| `2E` / `2F` | 46 / 47 | Extract / supply in extract-only mode (m³/h) | 0–100, 10 |
| `39` | 57 | Continuous ventilation level (m³/h) | 0–100, 10 |
| `53` | 83 | Unit operating hours | read-only |
| `54` | 84 | Fan motor operating hours | read-only |
| `65` | 101 | External switch mode (0 off, 1 airflow, 2 mode) | 0–3, 1 |

Notes:

- Airflow changes (e.g. Kennzahl 13) take effect within seconds.
- A switch-position mode written over RF (Kennzahl 16 = 10, extract-only) is
  stored and reported, but the unit doesn't apply it. Use
  `set_meltem_airflow` instead.
- Kennzahl 168–170 (external switch modes) get no reply over RF.
- **Never write Kennzahl 93 (`5D`): it is a factory reset.**
- Several IDs that ramses_cc polls for Orcon units get no reply from a Meltem
  unit, and the replies that do arrive are shown with Orcon names (e.g. `54` is
  shown as "Moisture sensor overrun time" but is fan motor operating hours).

## Filter reset

The *Reset filter counter* button (`W 10D0 00FF`) works once a remote is bound.
ramses_rf currently doesn't decode the Meltem filter days, because the unit
sends only the low byte of the days remaining and of the interval (about one
year).

## Not supported yet (ramses_rf)

These need a Meltem strategy in ramses_rf and can't be fixed in ramses_cc:

- 22F1 and 22F4 status frames are decoded with Orcon/Vasco names.
- 10D0 filter days remaining and interval.
- A Meltem parameter set for 2411 polling and entity names.
- A failed `W 2411` (no `I 2411` acknowledgement) isn't reported as an error.
