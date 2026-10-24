"""Датчики: статус передачи, деньги, поверка, принятые показания по счётчикам."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass, SensorEntity, SensorEntityDescription,
)
from homeassistant.const import EntityCategory, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import WATER_NAMES
from .coordinator import MvkConfigEntry, MvkCoordinator
from .entity import MvkEntity


def _date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _status(d: dict) -> str:
    if not d.get("active"):
        return "terminated"
    if d.get("sent_this_period"):
        return "sent"
    return "pending" if d.get("period_active") else "closed"


@dataclass(frozen=True, kw_only=True)
class MvkSensorDescription(SensorEntityDescription):
    value_fn: Callable[[dict], Any]
    attrs_fn: Callable[[dict], dict] | None = None


SENSORS: tuple[MvkSensorDescription, ...] = (
    MvkSensorDescription(
        key="readings_status", translation_key="readings_status", icon="mdi:clipboard-text-clock",
        device_class=SensorDeviceClass.ENUM, options=["sent", "pending", "closed", "terminated"],
        value_fn=_status,
        attrs_fn=lambda d: {k: d.get(k) for k in (
            "address", "period_start", "period_end", "period_date", "last_sent_period", "last_sent_at",
            "prebill_status")},
    ),
    MvkSensorDescription(
        key="holder", translation_key="holder", icon="mdi:account-tie",
        value_fn=lambda d: d.get("holder") or None,
        attrs_fn=lambda d: {k: d.get(k) for k in (
            "contract_date", "contract_state", "contract_type", "contract_since", "address", "object_type",
            "flat", "area", "residents", "owners", "delivery", "social_benefits")},
    ),
    MvkSensorDescription(
        key="tariff_supply", translation_key="tariff_supply", icon="mdi:water-plus",
        entity_category=EntityCategory.DIAGNOSTIC, native_unit_of_measurement="RUB/m³",
        value_fn=lambda d: d.get("tariff_supply"),
        attrs_fn=lambda d: {"basis": d.get("tariff_basis")},
    ),
    MvkSensorDescription(
        key="tariff_drainage", translation_key="tariff_drainage", icon="mdi:water-minus",
        entity_category=EntityCategory.DIAGNOSTIC, native_unit_of_measurement="RUB/m³",
        value_fn=lambda d: d.get("tariff_drainage"),
        attrs_fn=lambda d: {"basis": d.get("tariff_basis")},
    ),
    MvkSensorDescription(
        key="balance", translation_key="balance", icon="mdi:cash",
        device_class=SensorDeviceClass.MONETARY, native_unit_of_measurement="RUB",
        value_fn=lambda d: None if d.get("prepayment") is None
        else round((d.get("prepayment") or 0) - (d.get("debt") or 0), 2),
    ),
    MvkSensorDescription(
        key="debt", translation_key="debt", icon="mdi:cash-minus",
        device_class=SensorDeviceClass.MONETARY, native_unit_of_measurement="RUB",
        value_fn=lambda d: d.get("debt"),
    ),
    MvkSensorDescription(
        key="last_bill", translation_key="last_bill", icon="mdi:receipt-text",
        device_class=SensorDeviceClass.MONETARY, native_unit_of_measurement="RUB",
        value_fn=lambda d: d.get("last_bill_sum"),
        attrs_fn=lambda d: {"date": d.get("last_bill_date"), "status": d.get("last_bill_status")},
    ),
    MvkSensorDescription(
        key="period_end", translation_key="period_end", icon="mdi:calendar-end",
        device_class=SensorDeviceClass.DATE,
        value_fn=lambda d: _date(d.get("period_end")) if d.get("active") else None,
    ),
    MvkSensorDescription(
        key="last_sent_period", translation_key="last_sent_period", icon="mdi:calendar-check",
        device_class=SensorDeviceClass.DATE, value_fn=lambda d: _date(d.get("last_sent_period")),
    ),
    MvkSensorDescription(
        key="next_check", translation_key="next_check", icon="mdi:calendar-alert",
        device_class=SensorDeviceClass.DATE, entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: min((x for x in (_date(m.get("next_check")) for m in d.get("meters", {}).values())
                                if x), default=None),
    ),
)


async def async_setup_entry(hass: HomeAssistant, entry: MvkConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = []
    for contract, info in coordinator.data.items():
        entities += [MvkSensor(coordinator, contract, d) for d in SENSORS]
        entities += [MvkMeterSensor(coordinator, contract, serial) for serial in info.get("meters", {})]
    async_add_entities(entities)


class MvkSensor(MvkEntity, SensorEntity):
    entity_description: MvkSensorDescription

    def __init__(self, coordinator: MvkCoordinator, contract: str, description: MvkSensorDescription) -> None:
        super().__init__(coordinator, contract, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.info)

    @property
    def extra_state_attributes(self) -> dict | None:
        fn = self.entity_description.attrs_fn
        return fn(self.info) if fn else None


class MvkMeterSensor(MvkEntity, SensorEntity):
    """Последние показания счётчика, принятые Мосводоканалом."""

    _attr_native_unit_of_measurement = UnitOfVolume.CUBIC_METERS
    _attr_device_class = SensorDeviceClass.WATER
    _attr_translation_key = "meter"

    def __init__(self, coordinator: MvkCoordinator, contract: str, serial: str) -> None:
        super().__init__(coordinator, contract, f"meter_{serial}")
        self._serial = serial
        water = coordinator.data[contract]["meters"][serial].get("water")
        self._attr_translation_placeholders = {"serial": serial,
                                               "water": WATER_NAMES.get(water, water or "")}
        self._attr_icon = "mdi:water-thermometer" if water == "hot" else "mdi:water"

    @property
    def _meter(self) -> dict:
        return self.info.get("meters", {}).get(self._serial, {})

    @property
    def available(self) -> bool:
        return super().available and bool(self._meter)

    @property
    def native_value(self) -> float | None:
        return self._meter.get("value")

    @property
    def extra_state_attributes(self) -> dict:
        m = self._meter
        return {"serial": self._serial, "water": m.get("water"), "next_check": m.get("next_check"),
                "type": m.get("type")}
