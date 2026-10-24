"""Настройка: вход в кабинет (все договоры учётной записи), выбор датчиков для счётчиков."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers import selector

from .api import Account, MvkAuthError, MvkCaptchaError, MvkClient, MvkError, build_ssl_context
from .const import CONF_SOURCES, DOMAIN, WATER_NAMES
from .coordinator import MvkConfigEntry

USER_SCHEMA = vol.Schema({
    vol.Required(CONF_USERNAME): selector.TextSelector(
        selector.TextSelectorConfig(type=selector.TextSelectorType.EMAIL, autocomplete="username")),
    vol.Required(CONF_PASSWORD): selector.TextSelector(
        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD,
                                    autocomplete="current-password")),
})


class MvkConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 2

    async def _check(self, username: str, password: str, errors: dict[str, str]) -> list[Account] | None:
        ctx = await self.hass.async_add_executor_job(build_ssl_context)
        client = MvkClient(username, password, ctx)
        try:
            await client.login()
            return await client.accounts()
        except MvkCaptchaError:
            errors["base"] = "captcha"
        except MvkAuthError:
            errors["base"] = "invalid_auth"
        except MvkError:
            errors["base"] = "cannot_connect"
        finally:
            await client.close()
        return None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            accounts = await self._check(user_input[CONF_USERNAME], user_input[CONF_PASSWORD], errors)
            if accounts is not None:
                if not accounts:
                    return self.async_abort(reason="no_contracts")
                await self.async_set_unique_id(user_input[CONF_USERNAME].lower())
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=f"Мосводоканал ({user_input[CONF_USERNAME]})",
                                               data=user_input)
        return self.async_show_form(step_id="user", data_schema=self.add_suggested_values_to_schema(
            USER_SCHEMA, user_input), errors=errors)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            if await self._check(user_input[CONF_USERNAME], user_input[CONF_PASSWORD], errors) is not None:
                return self.async_update_reload_and_abort(entry, data_updates=user_input)
        return self.async_show_form(step_id="reauth_confirm", data_schema=self.add_suggested_values_to_schema(
            USER_SCHEMA, {CONF_USERNAME: entry.data[CONF_USERNAME]}), errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: MvkConfigEntry) -> OptionsFlow:
        return MvkOptionsFlow()


class MvkOptionsFlow(OptionsFlow):
    """Датчик HA для каждого счётчика действующих договоров. Поле: «договор_заводской номер»."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        data = self.config_entry.runtime_data.data or {}
        active = {c: d for c, d in data.items() if d.get("active")}
        if user_input is not None:
            sources: dict[str, dict[str, str]] = {}
            for key, entity_id in user_input.items():
                contract, _, serial = key.partition("_")
                if entity_id:
                    sources.setdefault(contract, {})[serial] = entity_id
            return self.async_create_entry(data={CONF_SOURCES: sources})
        current = self.config_entry.options.get(CONF_SOURCES, {})
        schema, lines = {}, []
        for contract, d in sorted(active.items()):
            lines.append(f"**Договор {contract}** — {d.get('address', '')}")
            for serial, m in sorted(d.get("meters", {}).items()):
                key = f"{contract}_{serial}"
                schema[vol.Optional(key, description={"suggested_value": current.get(contract, {}).get(serial)})] = \
                    selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))
                lines.append(f"- {key}: {WATER_NAMES.get(m.get('water'), m.get('water'))}, "
                             f"принято {m.get('value')} м³")
        if not schema:
            return self.async_abort(reason="no_active_contracts")
        return self.async_show_form(step_id="init", data_schema=vol.Schema(schema),
                                    description_placeholders={"meters": "\n".join(lines)})
