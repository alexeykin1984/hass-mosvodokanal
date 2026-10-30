"""Координатор: опрос всех договоров кабинета и передача показаний."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import MvkAuthError, MvkClient, MvkError, build_ssl_context
from .const import CONF_SOURCES, DOMAIN, RETRY_INTERVAL, UPDATE_INTERVAL, WATER_NAMES

_LOGGER = logging.getLogger(__name__)

type MvkConfigEntry = ConfigEntry[MvkCoordinator]


class MvkCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """data = {договор: {account_id, address, active, ...сводка}}."""

    config_entry: MvkConfigEntry

    def __init__(self, hass: HomeAssistant, entry: MvkConfigEntry) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=DOMAIN,
                         update_interval=UPDATE_INTERVAL)
        self._client: MvkClient | None = None

    async def _get_client(self) -> MvkClient:
        if self._client is None:
            ctx = await self.hass.async_add_executor_job(build_ssl_context)
            data = self.config_entry.data
            self._client = MvkClient(data[CONF_USERNAME], data[CONF_PASSWORD], ctx)
        return self._client

    async def async_shutdown(self) -> None:
        await super().async_shutdown()
        if self._client:
            await self._client.close()

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        # сбой → повтор через RETRY_INTERVAL, а не через 6 часов
        self.update_interval = RETRY_INTERVAL
        data = await self._fetch()
        self.update_interval = UPDATE_INTERVAL
        return data

    async def _fetch(self) -> dict[str, dict[str, Any]]:
        client = await self._get_client()
        try:
            await client.login()
            result: dict[str, dict[str, Any]] = {}
            for acc in await client.accounts():
                info: dict[str, Any] = {"account_id": acc.account_id, "contract": acc.contract,
                                        "address": acc.address, "active": acc.active, "meters": {}}
                try:
                    info.update(await client.contract_info(acc.account_id))
                except MvkError as err:
                    _LOGGER.debug("Договор %s: карточка недоступна: %s", acc.contract, err)
                try:
                    info.update(await client.status(acc.account_id))
                except MvkError as err:  # расторгнутый договор может отдавать не всё
                    _LOGGER.debug("Договор %s: сводка недоступна: %s", acc.contract, err)
                result[acc.contract] = info
            return result
        except MvkAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except MvkError as err:
            raise UpdateFailed(str(err)) from err
        finally:
            await client.close()

    @property
    def active_contracts(self) -> list[str]:
        return [c for c, d in (self.data or {}).items() if d.get("active")]

    def resolve_contract(self, contract: str | None) -> str:
        if contract:
            if contract not in (self.data or {}):
                raise HomeAssistantError(f"Договор {contract} не найден в кабинете")
            if not self.data[contract].get("active"):
                raise HomeAssistantError(f"Договор {contract} расторгнут — передача невозможна")
            return contract
        active = self.active_contracts
        if len(active) != 1:
            raise HomeAssistantError("Укажите договор: действующих договоров "
                                     f"{len(active)} ({', '.join(active) or 'нет'})")
        return active[0]

    def readings_from_sources(self, contract: str) -> tuple[dict[str, float], list[str]]:
        """Показания из датчиков HA, выбранных в настройках интеграции."""
        sources: dict[str, str] = self.config_entry.options.get(CONF_SOURCES, {}).get(contract, {})
        values, problems = {}, []
        for serial in self.data[contract].get("meters", {}):
            entity_id = sources.get(serial)
            if not entity_id:
                problems.append(f"для счётчика {serial} не выбран датчик (Настройки интеграции)")
                continue
            state = self.hass.states.get(entity_id)
            try:
                values[serial] = float(state.state)  # type: ignore[union-attr]
            except (AttributeError, TypeError, ValueError):
                problems.append(f"{entity_id}: нет показаний ({state.state if state else 'нет датчика'})")
        return values, problems

    async def async_send(self, contract: str | None, dry_run: bool,
                         readings: dict[str, float] | None = None, notify: bool = True) -> dict[str, Any]:
        """Передать показания (или только проверить). Результат — словарь и уведомление."""
        contract = self.resolve_contract(contract)
        if readings is None:
            readings, problems = self.readings_from_sources(contract)
            if problems:
                result = {"contract": contract, "dry_run": dry_run, "meters": [],
                          "error": "; ".join(problems)}
                if notify:
                    self._notify(result)
                return result
        client = await self._get_client()
        try:
            await client.login()
            result = await client.send_readings(self.data[contract]["account_id"], contract,
                                                {str(k): float(v) for k, v in readings.items()},
                                                dry_run=dry_run)
        except MvkAuthError as err:
            self.config_entry.async_start_reauth(self.hass)
            raise HomeAssistantError(f"Мосводоканал: {err}") from err
        except MvkError as err:
            raise HomeAssistantError(f"Мосводоканал: {err}") from err
        finally:
            await client.close()
        if notify:
            self._notify(result)
        if not dry_run and result.get("ok"):
            await self.async_request_refresh()
        return result

    def _notify(self, r: dict[str, Any]) -> None:
        if r.get("error"):
            title = "Мосводоканал — показания не переданы"
        elif r.get("dry_run"):
            title = "Мосводоканал — проверка (ничего не отправлено)"
        else:
            title = "Мосводоканал — показания переданы"
        lines = [f"Договор {r.get('contract')}"
                 + (f", приём показаний {r['period_start']} — {r['period_end']}." if r.get("period_start") else ".")]
        if r.get("error"):
            lines.append(f"\n**{r['error']}**\n")
        for m in r.get("meters", []):
            water = WATER_NAMES.get(m.get("water"), m.get("water") or "")
            lines.append(f"- №{m['serial']} ({water}): **{m['value']}** м³ (было {int(m['prev'] or 0)})")
        resp = r.get("response")
        if isinstance(resp, dict):
            lines.append(f"\nОтвет Мосводоканала: {resp.get('requestStatus', '?')}"
                         + (f", не принято: {resp['invalidCount']}" if resp.get("invalidCount") else ""))
        persistent_notification.async_create(self.hass, "\n".join(lines), title,
                                             notification_id=f"{DOMAIN}_{r.get('contract')}")
