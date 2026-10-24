"""Кнопки: обновить данные, проверить и передать показания."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import MvkConfigEntry, MvkCoordinator
from .entity import MvkEntity


async def async_setup_entry(hass: HomeAssistant, entry: MvkConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    c = entry.runtime_data
    buttons: list[MvkButton] = []
    for contract, info in c.data.items():
        buttons.append(MvkButton(c, contract, "refresh", "mdi:refresh"))
        if info.get("active"):  # передавать можно только по действующим договорам
            buttons += [MvkButton(c, contract, "check", "mdi:clipboard-check-outline"),
                        MvkButton(c, contract, "send", "mdi:send")]
    async_add_entities(buttons)


class MvkButton(MvkEntity, ButtonEntity):
    def __init__(self, coordinator: MvkCoordinator, contract: str, key: str, icon: str) -> None:
        super().__init__(coordinator, contract, f"button_{key}")
        self._key = key
        self._attr_translation_key = key
        self._attr_icon = icon

    async def async_press(self) -> None:
        if self._key == "refresh":
            await self.coordinator.async_request_refresh()
        else:
            await self.coordinator.async_send(self.contract, dry_run=self._key == "check")
