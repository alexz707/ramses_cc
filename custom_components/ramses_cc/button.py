"""Support for RAMSES RF button entities.

Adapted from number.py

Buttons are stateless momentary entities that trigger an action when
pressed. This module provides:

- gateway-level buttons that invoke the integration's own domain-wide
  services (see ./services.yaml), and
- per-FAN buttons that reset the filter counter (W 10D0) via the
  ``reset_filter_counter`` entity service (see ./remote.py), and
- per-FAN pairing buttons that create a faked remote for the FAN and
  bind it (``add_faked_rem`` / ``bind_device``).

The button platform may be set up before the HGIs are loaded or before
the remote platform has registered the FAN REM entities. A discovery
callback is therefore registered with the coordinator: as devices
(re)appear, any missing buttons are created once their REM entity
exists.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Final

from homeassistant.components.button import (
    ButtonEntity,
    ButtonEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import (
    entity_platform,
    entity_registry as er,
)
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity_platform import (
    AddEntitiesCallback,
    EntityPlatform,
)

from ramses_rf.const import DevType
from ramses_rf.devices import HgiGateway, HvacVentilator
from ramses_rf.entity import Entity as RamsesRFEntity

from .const import (
    CONF_SCHEMA,
    DOMAIN,
    SIGNAL_NEW_DEVICES,
    SVC_ADD_FAKED_REM,
    SVC_DISCOVER_KNOWN_DEVICES,
    SZ_TR_ALIAS,
    SZ_TR_BOUND,
    SZ_TR_FAKED,
)
from .entity import RamsesEntity, RamsesEntityDescription
from .helpers import (
    configured_hvac_strategy,
    device_slug,
    normalize_device_id,
)
from .schemas import (
    SVC_BIND_DEVICE,
    SVC_FORCE_UPDATE,
    SVC_RESET_FILTER,
    SVC_SYNC_TOPOLOGY,
)
from .typing import RamsesConfigEntry

if TYPE_CHECKING:
    from .coordinator import RamsesCoordinator

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class RamsesButtonEntityDescription(
    RamsesEntityDescription, ButtonEntityDescription
):
    """Class describing Ramses button entities."""

    service: str | None = None  # ramses_cc service to call when pressed
    service_data: dict[str, str] | None = None
    target: dict[str, Any] | None = None
    entity_category: EntityCategory | None = None


# Gateway-level service buttons created for each HGI.
HGI_BUTTON_DESCRIPTIONS: tuple[RamsesButtonEntityDescription, ...] = (
    RamsesButtonEntityDescription(
        key="force_update",
        translation_key="force_update",
        icon="mdi:refresh",
        service=SVC_FORCE_UPDATE,
    ),
    RamsesButtonEntityDescription(
        key="sync_topology",
        translation_key="sync_topology",
        icon="mdi:lan-connect",
        service=SVC_SYNC_TOPOLOGY,
    ),
    RamsesButtonEntityDescription(
        key="discover_known_devices",
        translation_key="discover_known_devices",
        icon="mdi:magnify",
        service=SVC_DISCOVER_KNOWN_DEVICES,
    ),
)

FILTER_RESET_KEY = "reset_filter_counter"
CREATE_REMOTE_KEY = "create_remote"
BIND_REMOTE_KEY = "bind_remote"

#: Binding offer when the FAN has no strategy (the Orcon/Itho/Vasco set)
_DEFAULT_BINDING_CODES: Final = ("22F1", "22F3")
#: add_faked_rem accepts an alias of at most 50 characters
_MAX_ALIAS_LEN: Final = 50


class RamsesButtonBase(RamsesEntity, ButtonEntity):
    """Base for any RAMSES II-compatible button entity."""

    entity_description: RamsesButtonEntityDescription

    _attr_has_entity_name = True

    async def async_press(self) -> None:
        """Handle the button press.

        Calls the ramses_cc service named in the entity description,
        passing any configured service data (e.g. the entity_id of the
        FAN remote entity for the filter reset).
        """
        service = self.entity_description.service
        if not service:
            _LOGGER.warning(
                "Button %s has no service configured", self.entity_id
            )
            return

        _LOGGER.debug(
            "Button %s calls service %s (data=%s, target=%s)",
            self.entity_id,
            service,
            self.entity_description.service_data,
            self.entity_description.target,
        )
        await self.hass.services.async_call(
            DOMAIN,
            service,
            service_data=self.entity_description.service_data or {},
            target=self.entity_description.target or {},
            blocking=True,
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the target attribute, plus any base attributes."""
        return super().extra_state_attributes | {
            "target": self.entity_description.target,
        }


class RamsesFanPairingButton(RamsesButtonBase):
    """FAN button that creates or binds the FAN's faked remote.

    Pairing a faked remote with a FAN takes two steps, which these
    buttons put on the FAN's device page:

    - ``create_remote``: create a faked REM bound to the FAN, with the
      FAN's address in the ``37:`` range (``32:150093`` →
      ``37:150093``).  This only changes the schema; nothing is sent.
    - ``bind_remote``: send the binding offer from the FAN's bound,
      faked REM, using the codes of the FAN's HVAC strategy.  The FAN
      only accepts it in its pairing window (e.g. 5 minutes after a
      Meltem unit is powered on).
    """

    @property
    def available(self) -> bool:
        """Return True: pairing works while the FAN isn't heard.

        Create remote doesn't talk to the FAN, and a FAN that was just
        power-cycled for binding may not have been heard yet.

        :return: Always True.
        """
        return True

    def _schema(self) -> dict[str, Any]:
        """Return the system schema from the config entry options."""
        return dict(self.coordinator.options.get(CONF_SCHEMA, {}))

    def _bound_rem_id(self) -> str | None:
        """Return the FAN's bound REM, from ramses_rf or the schema.

        A REM just created by ``create_remote`` is in the schema before
        ramses_rf has loaded it, so the schema is checked too.
        """
        getter = getattr(self._device, "get_bound_rem", None)
        if callable(getter) and (rem_id := getter()):
            return str(rem_id)
        bound = self._schema().get(self._device.id, {}).get(SZ_TR_BOUND)
        return str(bound) if bound else None

    def _is_faked(self, rem_id: str) -> bool:
        """Return True if the REM is faked (in ramses_rf or the schema)."""
        if (rem := self.coordinator.get_device(rem_id)) is not None:
            return bool(rem.is_faked)
        return bool(self._schema().get(rem_id, {}).get(SZ_TR_FAKED))

    async def async_press(self) -> None:
        """Create or bind the FAN's faked remote.

        :raises HomeAssistantError: If the step doesn't apply to the FAN
            (e.g. it already has a remote, or has no faked remote to bind).
        """
        if self.entity_description.key == CREATE_REMOTE_KEY:
            await self._async_create_remote()
        else:
            await self._async_bind_remote()

    async def _async_create_remote(self) -> None:
        """Create a faked REM bound to the FAN."""
        fan_id = self._device.id
        if bound := self._bound_rem_id():
            raise HomeAssistantError(
                f"FAN {fan_id} already has a remote ({bound}); "
                "use Bind remote to pair it"
            )
        rem_id = f"37:{fan_id.split(':', 1)[1]}"
        if self.coordinator.get_device(rem_id) is not None or (
            rem_id in self._schema()
        ):
            raise HomeAssistantError(
                f"Address {rem_id} is already in use; create the remote "
                "with the add_faked_rem action and another 37: address"
            )
        fan_alias = self._schema().get(fan_id, {}).get(SZ_TR_ALIAS) or fan_id
        alias = f"Virtual REM {fan_alias}"[:_MAX_ALIAS_LEN]
        _LOGGER.info("Creating faked REM %s for FAN %s", rem_id, fan_id)
        await self.hass.services.async_call(
            DOMAIN,
            SVC_ADD_FAKED_REM,
            service_data={
                "device_id": rem_id,
                "bound_to": fan_id,
                "alias": alias,
            },
            blocking=True,
        )
        await self._async_apply_binding()

    async def _async_bind_remote(self) -> None:
        """Send the binding offer from the FAN's faked REM."""
        fan_id = self._device.id
        rem_id = self._bound_rem_id()
        if rem_id is None:
            raise HomeAssistantError(
                f"FAN {fan_id} has no remote to bind; press Create remote "
                "first"
            )
        if not self._is_faked(rem_id):
            raise HomeAssistantError(
                f"Remote {rem_id} of FAN {fan_id} is a real remote; only a "
                "faked remote can be bound from Home Assistant"
            )
        strategy = configured_hvac_strategy(self._device)
        codes = (
            tuple(str(code) for code in strategy.binding_codes())
            if strategy is not None
            else _DEFAULT_BINDING_CODES
        )
        offer = {code: "00" for code in codes}
        _LOGGER.info(
            "Binding faked REM %s to FAN %s (offer %s)", rem_id, fan_id, offer
        )
        await self.hass.services.async_call(
            DOMAIN,
            SVC_BIND_DEVICE,
            service_data={"device_id": rem_id, "offer": offer},
            blocking=True,
        )
        await self._async_apply_binding()

    async def _async_apply_binding(self) -> None:
        """Make the FAN use its new remote without a reload.

        ``add_faked_rem`` only writes the schema, and ramses_rf binds a
        FAN to its REM when the FAN is set up, so the FAN would only use
        the new remote after a reload of the integration.  Re-run that
        setup for this FAN, then re-announce the FAN to the button
        platform so its Reset filter counter button is created once the
        remote is faked (after Bind remote).
        """
        if not isinstance(self._device, HvacVentilator):
            return
        await self.coordinator.fan_handler.setup_fan_bound_devices(
            self._device
        )
        async_dispatcher_send(
            self.hass, SIGNAL_NEW_DEVICES.format("button"), [self._device]
        )


#: Pairing buttons on every FAN device page
FAN_PAIRING_DESCRIPTIONS: tuple[RamsesButtonEntityDescription, ...] = (
    RamsesButtonEntityDescription(
        key=CREATE_REMOTE_KEY,
        translation_key=CREATE_REMOTE_KEY,
        icon="mdi:remote",
        entity_category=EntityCategory.CONFIG,
    ),
    RamsesButtonEntityDescription(
        key=BIND_REMOTE_KEY,
        translation_key=BIND_REMOTE_KEY,
        icon="mdi:link-variant",
        entity_category=EntityCategory.CONFIG,
    ),
)


def _fan_remote_entity(
    ent_reg: er.EntityRegistry, entry_id: str, fan_id: str
) -> er.RegistryEntry | None:
    """Return the registry entry of the remote entity on a FAN device.

    The ``reset_filter_counter`` entity service accepts a FAN REM
    entity_id as its target (the handler resolves the bound REM itself).
    """
    return next(
        (
            entity
            for entity in er.async_entries_for_config_entry(ent_reg, entry_id)
            if entity.domain == "remote" and entity.unique_id == str(fan_id)
        ),
        None,
    )


class _ButtonFactory:
    """Creates button entities for newly discovered devices.

    Tracks unique_ids to prevent duplicate entities across the initial
    setup pass and subsequent coordinator discovery callbacks.
    """

    def __init__(self, coordinator: RamsesCoordinator) -> None:
        self._coordinator = coordinator
        self._hass = coordinator.hass
        self._entry_id = coordinator.entry.entry_id
        self._known_unique_ids: set[str] = set()

    def hgi_buttons(self, hgi: RamsesRFEntity) -> list[RamsesButtonBase]:
        """Create gateway service buttons for an HGI device (deduped)."""
        if not isinstance(hgi, HgiGateway) or hgi.id is None:
            return []

        device_id = normalize_device_id(hgi.id)
        _LOGGER.debug("Adding HGI buttons for %s", device_id)

        return [
            button
            for description in HGI_BUTTON_DESCRIPTIONS
            if (button := self._make_button(hgi, description, device_id))
        ]

    def pairing_buttons(self, fan: RamsesRFEntity) -> list[RamsesButtonBase]:
        """Create the Create remote / Bind remote buttons for a FAN.

        Created for every FAN: whether a step applies (no remote yet, or
        a faked remote to bind) is checked when the button is pressed.
        """
        if not isinstance(fan, HvacVentilator) or fan.id is None:
            return []
        device_id = normalize_device_id(fan.id)
        return [
            button
            for description in FAN_PAIRING_DESCRIPTIONS
            if (
                button := self._make_button(
                    fan, description, device_id, RamsesFanPairingButton
                )
            )
        ]

    def fan_buttons(self, fan: RamsesRFEntity) -> list[RamsesButtonBase]:
        """Create a filter-reset button for a FAN device (deduped).

        Returns an empty list (to be retried on the next discovery
        callback) if the FAN has no bound REM yet or the REM entity has
        not yet been registered by the remote platform. A FAN bound to
        a non-faked REM gets no button at all.
        """
        if not isinstance(fan, HvacVentilator):
            return []

        if hasattr(fan, "supports_10d0"):
            if not getattr(fan, "supports_10d0", False):
                _LOGGER.debug(
                    "Device %s does not seem to support 10D0 Filter Change",
                    fan.id,
                )
                return []

        rem_id = fan.get_bound_rem()
        if rem_id is None:
            _LOGGER.debug(
                "No bound REM for FAN %s; will retry when devices are"
                " (re)discovered",
                fan.id,
            )
            return []

        # Only faked REMs can transmit (real REMs can't be impersonated),
        # so a FAN bound to a real REM gets no button.
        rem_dev = self._coordinator.get_device(rem_id)
        if rem_dev is not None and not rem_dev.is_faked:
            _LOGGER.debug(
                "Bound REM %s is not faked; no reset button for FAN %s",
                rem_id,
                fan.id,
            )
            return []

        remote_entity = _fan_remote_entity(
            er.async_get(self._hass), self._entry_id, fan.id
        )
        if remote_entity is None:
            _LOGGER.debug(
                "No remote entity yet for FAN %s; will retry when devices"
                " are (re)discovered",
                fan.id,
            )
            return []

        _LOGGER.debug(
            "Preparing filter counter reset button, targeting REM %s on"
            " FAN %s",
            remote_entity.entity_id,
            fan.id,
        )

        button = self._make_button(
            fan,
            RamsesButtonEntityDescription(
                key=FILTER_RESET_KEY,
                translation_key=FILTER_RESET_KEY,
                icon="mdi:restart-alert",
                service=SVC_RESET_FILTER,
                target={"entity_id": [remote_entity.entity_id]},
            ),
            normalize_device_id(fan.id),
        )
        if button is None:
            return []

        return [button]

    def button_entities(
        self, device: RamsesRFEntity
    ) -> list[RamsesButtonBase]:
        """Create all applicable buttons for a single device."""
        match device_slug(device):
            case DevType.HGI:
                return self.hgi_buttons(device)
            case "FAN":
                return self.pairing_buttons(device) + self.fan_buttons(device)
            case _:
                return []

    def _make_button(
        self,
        device: RamsesRFEntity,
        description: RamsesButtonEntityDescription,
        device_id: str,
        button_cls: type[RamsesButtonBase] = RamsesButtonBase,
    ) -> RamsesButtonBase | None:
        """Create a button unless its unique_id was already created."""
        unique_id = f"{device_id}-{description.key}"
        if unique_id in self._known_unique_ids:
            _LOGGER.debug("Button %s already created, skipping", unique_id)
            return None

        # hass.services.async_call rejects a set as entity_id target, so
        # coerce any set to a sorted list once, here (single choke point
        # for every description, current or future).
        if description.target and isinstance(
            description.target.get("entity_id"), set
        ):
            description = replace(
                description,
                target=description.target
                | {"entity_id": sorted(description.target["entity_id"])},
            )

        self._known_unique_ids.add(unique_id)
        button = button_cls(self._coordinator, device, description)
        button._attr_unique_id = unique_id
        return button


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RamsesConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the RAMSES button platform from a config entry.

    Creates one gateway-level button per domain-wide service action,
    plus one filter-reset button per FAN device. Devices discovered
    after setup (or whose REM entity was not yet registered) are
    handled by the discovery callback registered with the coordinator.
    """
    coordinator: RamsesCoordinator = entry.runtime_data
    platform: EntityPlatform = entity_platform.async_get_current_platform()
    factory = _ButtonFactory(coordinator)

    @callback
    def add_devices(
        devices: RamsesRFEntity
        | RamsesButtonBase
        | Sequence[RamsesRFEntity | RamsesButtonBase],
    ) -> None:
        """Add button entities for the given devices or entities.

        Devices arrive via the coordinator's new-device dispatch;
        prebuilt entities arrive from the setup pass below. Both are
        deduplicated via the factory's known unique_ids and the
        platform's loaded entities.

        :param devices: Devices or button entities to process.
        """
        device_list = devices if isinstance(devices, Sequence) else [devices]
        if not device_list:
            return

        if all(isinstance(d, RamsesButtonBase) for d in device_list):
            # Entities passed directly: add the ones not already
            # loaded by this platform.
            entities_to_add = [
                entity
                for entity in device_list
                if isinstance(entity, RamsesButtonBase)
                and entity.entity_id not in platform.entities
            ]
            if entities_to_add:
                async_add_entities(entities_to_add)
            return

        for device in device_list:
            if not isinstance(device, RamsesRFEntity):
                _LOGGER.debug("Skipping non-device item: %s", device)
                continue
            if _buttons := factory.button_entities(device):
                async_add_entities(_buttons)

    coordinator.async_register_platform(platform, add_devices)

    # Buttons for devices already known at setup time; anything
    # discovered later (or still missing its REM entity) is handled by
    # the callback above.
    entities: list[RamsesButtonBase] = [
        button
        for device in coordinator.devices
        if isinstance(device, RamsesRFEntity)
        for button in factory.button_entities(device)
    ]
    if entities:
        _LOGGER.debug("Adding %d button entities", len(entities))
        add_devices(entities)
    else:
        _LOGGER.debug("No button entities registered at setup time")
