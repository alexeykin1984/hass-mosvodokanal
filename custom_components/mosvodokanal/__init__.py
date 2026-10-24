"""Интеграция с личным кабинетом АО «Мосводоканал» (физлица, onewind.mosvodokanal.ru)."""
from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.const import CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import (
    ATTR_CONTRACT, ATTR_DRY_RUN, ATTR_ENTRY_ID, ATTR_READINGS, CONF_ACCOUNT_ID, CONF_ADDRESS,
    CONF_CONTRACT, CONF_SOURCES, DOMAIN, SERVICE_SEND,
)
from .coordinator import MvkConfigEntry, MvkCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.BUTTON]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SEND_SCHEMA = vol.Schema({
    vol.Optional(ATTR_ENTRY_ID): cv.string,
    vol.Optional(ATTR_CONTRACT): cv.string,
    vol.Optional(ATTR_DRY_RUN, default=False): cv.boolean,
    vol.Optional(ATTR_READINGS): vol.Schema({cv.string: vol.Coerce(float)}),
})


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async def handle_send(call: ServiceCall) -> ServiceResponse:
        entries = hass.config_entries.async_loaded_entries(DOMAIN)
        if entry_id := call.data.get(ATTR_ENTRY_ID):
            entries = [e for e in entries if e.entry_id == entry_id]
        contract = call.data.get(ATTR_CONTRACT)
        if contract:
            entries = [e for e in entries if contract in (e.runtime_data.data or {})]
        if len(entries) != 1:
            raise ServiceValidationError("Не удалось однозначно выбрать учётную запись: "
                                         "укажите config_entry_id или contract")
        coordinator: MvkCoordinator = entries[0].runtime_data
        return await coordinator.async_send(contract, call.data[ATTR_DRY_RUN], call.data.get(ATTR_READINGS))

    hass.services.async_register(DOMAIN, SERVICE_SEND, handle_send, schema=SEND_SCHEMA,
                                 supports_response=SupportsResponse.OPTIONAL)
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: MvkConfigEntry) -> bool:
    """v1: запись = один договор → v2: запись = учётная запись кабинета со всеми договорами."""
    if entry.version == 1:
        data = dict(entry.data)
        contract = data.pop(CONF_CONTRACT, None)
        data.pop(CONF_ACCOUNT_ID, None)
        data.pop(CONF_ADDRESS, None)
        options = dict(entry.options)
        sources = options.get(CONF_SOURCES, {})
        if contract and sources and all(isinstance(v, str) for v in sources.values()):
            options[CONF_SOURCES] = {contract: sources}
        hass.config_entries.async_update_entry(
            entry, data=data, options=options, version=2,
            unique_id=data[CONF_USERNAME].lower(), title=f"Мосводоканал ({data[CONF_USERNAME]})")
        _LOGGER.info("Мосводоканал: запись перенесена на версию 2")
    return True


async def async_setup_entry(hass: HomeAssistant, entry: MvkConfigEntry) -> bool:
    coordinator = MvkCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: HomeAssistant, entry: MvkConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: MvkConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
