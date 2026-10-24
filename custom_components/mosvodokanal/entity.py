"""Общая база сущностей: одно устройство-«служба» на договор."""
from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import BASE_URL
from .const import DOMAIN
from .coordinator import MvkCoordinator


class MvkEntity(CoordinatorEntity[MvkCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: MvkCoordinator, contract: str, key: str) -> None:
        super().__init__(coordinator)
        self.contract = contract
        self._attr_unique_id = f"{contract}_{key}"
        info = coordinator.data[contract]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, contract)},
            name=f"Мосводоканал {contract}",
            manufacturer="АО «Мосводоканал»",
            model=info.get("holder") or ("Договор действует" if info.get("active") else "Договор расторгнут"),
            model_id=info.get("address") or None,
            serial_number=contract,
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=BASE_URL,
        )

    @property
    def info(self) -> dict[str, Any]:
        return (self.coordinator.data or {}).get(self.contract, {})

    @property
    def available(self) -> bool:
        return super().available and bool(self.info)
