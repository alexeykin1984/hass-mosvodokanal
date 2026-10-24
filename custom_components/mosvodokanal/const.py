"""Константы интеграции Мосводоканал."""
from datetime import timedelta

DOMAIN = "mosvodokanal"

CONF_SOURCES = "sources"  # опции: {договор: {заводской номер: entity_id датчика HA}}
# устаревшие ключи записи версии 1 (один договор на запись)
CONF_CONTRACT = "contract"
CONF_ACCOUNT_ID = "account_id"
CONF_ADDRESS = "address"

UPDATE_INTERVAL = timedelta(hours=6)

SERVICE_SEND = "send_readings"
ATTR_DRY_RUN = "dry_run"
ATTR_READINGS = "readings"
ATTR_ENTRY_ID = "config_entry_id"
ATTR_CONTRACT = "contract"

WATER_NAMES = {"hot": "горячая", "cold": "холодная"}
