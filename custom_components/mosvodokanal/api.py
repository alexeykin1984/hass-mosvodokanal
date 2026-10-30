"""Клиент личного кабинета физлица АО «Мосводоканал» (onewind.mosvodokanal.ru).

Протокол восстановлен по фронтенду кабинета (ExtJS). Все запросы идут на /api/<команда>.
"""
from __future__ import annotations

import gzip
import json
import socket
import ssl
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp

BASE_URL = "https://onewind.mosvodokanal.ru/"
API_URL = BASE_URL + "api/"
REQUEST_TIMEOUT = 180  # секунд на один запрос

# Сайт не отдаёт промежуточный сертификат GlobalSign GCC R3 DV TLS CA 2020 — кладём его рядом.
INTERMEDIATE_CERT = Path(__file__).with_name("gsgccr3dvtlsca2020.pem")


class MvkError(Exception):
    """Ошибка обмена с кабинетом."""


class MvkAuthError(MvkError):
    """Неверный логин или пароль."""


class MvkCaptchaError(MvkAuthError):
    """Кабинет требует капчу (после нескольких неудачных входов)."""


@dataclass
class Account:
    account_id: str
    contract: str
    address: str
    active: bool


def build_ssl_context() -> ssl.SSLContext:
    """Системные корневые сертификаты + недостающий промежуточный. Блокирующая функция."""
    import certifi

    ctx = ssl.create_default_context(cafile=certifi.where())
    ctx.load_verify_locations(cafile=str(INTERMEDIATE_CERT))
    return ctx


def _num(value: Any) -> float | None:
    try:
        return round(float(str(value).replace(",", ".")), 2)
    except (TypeError, ValueError):
        return None


class MvkClient:
    """Одна сессия кабинета. Перед серией запросов вызывайте login()."""

    def __init__(self, login: str, password: str, ssl_context: ssl.SSLContext) -> None:
        self._login = login
        self._password = password
        self._ssl = ssl_context
        self._session: aiohttp.ClientSession | None = None

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _new_session(self) -> aiohttp.ClientSession:
        await self.close()
        # DNS mosvodokanal.ru отвечает REFUSED на AAAA-запросы — только IPv4.
        connector = aiohttp.TCPConnector(family=socket.AF_INET, ssl=self._ssl)
        self._session = aiohttp.ClientSession(
            connector=connector,
            # в конце периода приёма отчёты кабинета отвечают по 30–60 с и дольше
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            headers={
                "X-Requested-With": "XMLHttpRequest",
                # фронтенд шлёт со всеми запросами случайный UUID
                "X-CSRF-Token": str(uuid.uuid4()),
            },
        )
        return self._session

    @staticmethod
    async def _decode(resp: aiohttp.ClientResponse) -> Any:
        body = await resp.read()
        if body[:2] == b"\x1f\x8b":  # бывает gzip без Content-Encoding
            body = gzip.decompress(body)
        text = body.decode("utf-8", "replace")
        try:
            return json.loads(text)
        except ValueError:
            return text

    async def _call(self, command: str, data: dict | None = None, json_body: Any = None,
                    method: str = "POST") -> Any:
        if self._session is None:
            raise MvkError("нет сессии, сначала login()")
        try:
            async with self._session.request(method, API_URL + command, data=data,
                                             json=json_body) as resp:
                if resp.status in (401, 403):
                    raise MvkAuthError(f"{command}: HTTP {resp.status}")
                if resp.status >= 400:
                    raise MvkError(f"{command}: HTTP {resp.status}")
                return await self._decode(resp)
        except TimeoutError as err:
            raise MvkError(f"{command}: сайт не ответил за {REQUEST_TIMEOUT} с") from err
        except aiohttp.ClientError as err:
            raise MvkError(f"{command}: {err}") from err

    async def login(self) -> dict:
        session = await self._new_session()
        try:
            async with session.get(BASE_URL) as resp:  # cookie сессии, как в браузере
                await resp.read()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise MvkError(f"сайт недоступен: {err or 'таймаут'}") from err
        res = await self._call("login", {"username": self._login, "password": self._password,
                                         "captcha": ""})
        if isinstance(res, dict) and res.get("authenticated") is True:
            return res
        if isinstance(res, dict) and res.get("showCaptcha"):
            raise MvkCaptchaError("кабинет требует капчу — войдите один раз через браузер")
        raise MvkAuthError("неверный логин или пароль")

    async def report(self, report_id: str, **filters: Any) -> list[dict]:
        params = [{"type": k, "value": str(v)} for k, v in filters.items()]
        res = await self._call("ReportFormData/get", {
            "reportId": report_id, "parameterList": json.dumps(params), "start": 0, "limit": 100})
        return res.get("list", []) if isinstance(res, dict) else []

    async def accounts(self) -> list[Account]:
        register = await self.report("NATURAL_PERSON_USER_ACCOUNT_REGISTER")
        active = {a.get("VALUE"): a.get("DESCRIPTION", "") for a in await self.report("ACTIVE_USER_ACCOUNT")}
        return [
            Account(
                account_id=a["ID"],
                contract=a.get("DOG_TAG", ""),
                address=a.get("ADR_NAME_FULL", ""),
                active=a["ID"] in active and "Расторгнут" not in active[a["ID"]],
            )
            for a in register
        ]

    async def contract_info(self, account_id: str) -> dict:
        """Карточка договора: кто заключил, дата, тип, тарифы, проживающие, площадь."""
        row = (await self.report("NATURAL_PERSON_ACCOUNT_INFO", ACCOUNT_ID=account_id) or [{}])[0]
        holder = (row.get("CLI_ZAK_NAME") or "").strip()
        return {
            "holder": holder.title() if holder.isupper() else holder,
            "contract_date": row.get("DOG_DATE"),
            "contract_state": row.get("DOG_SOST"),
            "contract_type": " ".join((row.get("DOG_TYPE") or "").split()) or None,
            "contract_since": row.get("BEG_DATE"),
            "object_type": row.get("OBJ_TYPE_DESCR"),
            "flat": row.get("FLAT_NUMBER"),
            "area": _num(row.get("SQUARE")),
            "residents": int(row["POPULATION"]) if str(row.get("POPULATION", "")).isdigit() else None,
            "owners": int(row["OWNER_COUNT"]) if str(row.get("OWNER_COUNT", "")).isdigit() else None,
            "delivery": row.get("DOG_TYPE_DELIVERY_DOC_NAME"),
            "social_benefits": row.get("SOCIAL_BENEFITS_EXISTS") == "Y",
            "tariff_supply": _num(row.get("TAX_VP")),     # водоснабжение, ₽/м³
            "tariff_drainage": _num(row.get("TAX_VO")),   # водоотведение, ₽/м³
            "tariff_basis": row.get("TAX_REMARK") if row.get("TAX_REMARK") not in (None, "-") else None,
        }

    async def period(self) -> dict:
        res = await self._call("GetActivePeriod/readActivePeriod", method="GET")
        return res if isinstance(res, dict) else {}

    async def status(self, account_id: str) -> dict:
        """Сводка по договору: период, баланс, история передачи, счета, счётчики."""
        period = await self.period()
        debt = (await self.report("USER_ACCOUNT_DEBT", ACCOUNT_ID=account_id) or [{}])[0]
        history = await self.report("NATURAL_PERSON_METER_READ_REQUEST_HISTORY", ACCOUNT_ID=account_id)
        sent = sorted((h for h in history if h.get("REQUEST_STATUS_CD") in ("REGISTERED", "POSTED")),
                      key=lambda h: h.get("METER_READ_DATE") or "", reverse=True)
        bills = sorted(await self.report("NATURAL_PERSON_ACCOUNT_BILL_REGISTER", ACCOUNT_ID=account_id),
                       key=lambda b: b.get("DOC_DATE") or "", reverse=True)
        meters = await self.report("ACCOUNT_METER_REGISTER", ACCOUNT_ID=account_id)
        period_date = period.get("defaultMeterReadDate")
        last = sent[0] if sent else {}
        bill = bills[0] if bills else {}
        return {
            "period_active": bool(period.get("active")),
            "period_start": period.get("periodStart"),
            "period_end": period.get("periodEnd"),
            "period_date": period_date,
            "sent_this_period": any(h.get("METER_READ_DATE") == period_date for h in sent),
            "debt": _num(debt.get("DEBT_AMT")),
            "prepayment": _num(debt.get("PREPAYMENT_AMT")),
            "prebill_status": debt.get("MR_PREBILL_STATUS"),
            "last_sent_period": last.get("METER_READ_DATE"),
            "last_sent_at": last.get("POSTED_DTTM"),
            "last_bill_date": bill.get("DOC_DATE"),
            "last_bill_sum": _num(bill.get("CGS_SUM")),
            "last_bill_status": bill.get("VST_DOC_STATUS_DESCR"),
            "meters": {
                m.get("PRODUCTION_TAG"): {
                    "serial": m.get("PRODUCTION_TAG"),
                    "water": m.get("WATER_TYPE_ABBR"),  # hot / cold
                    "value": _num(m.get("METER_READING_VALUE")),
                    "next_check": m.get("NEXT_CHECK_DATE"),
                    "type": m.get("TYPE_NAME"),
                }
                for m in meters if m.get("PRODUCTION_TAG")
            },
        }

    async def send_readings(self, account_id: str, contract: str, values: dict[str, float],
                            dry_run: bool = False) -> dict:
        """Передать показания (целые м³, округление вниз). Проверяет период, полноту и рост."""
        out: dict[str, Any] = {"contract": contract, "dry_run": dry_run, "meters": []}
        period = await self.period()
        out["period_start"], out["period_end"] = period.get("periodStart"), period.get("periodEnd")
        if not period.get("active"):
            out["error"] = "период приёма показаний закрыт"
            return out
        request = await self._call("NewMeterReadRequest/getOrCreateMeterReadRequest",
                                   {"accountId": account_id})
        request_id = request.get("requestId") if isinstance(request, dict) else None
        if not request_id:
            out["error"] = "кабинет не выдал заявку на передачу показаний"
            return out
        rows = await self.report("NATURAL_PERSON_METER_READ_REQUEST_PART_DATA_REESTR",
                                 REQUEST_ID=request_id)
        beans, problems = [], []
        info = f"Показания по договору №{contract} : \n"
        for row in rows:
            serial = row.get("PRODUCTION_TAG")
            prev = _num(row.get("PREV_METER_READING_VALUE")) or 0
            value = int(values[serial]) if serial in values else None
            if value is None:
                problems.append(f"нет показаний для счётчика {serial}")
            elif value < prev:
                problems.append(f"{serial}: {value} меньше прошлых {int(prev)}")
            out["meters"].append({"serial": serial, "water": row.get("WATER_TYPE_ABBR"),
                                  "prev": prev, "value": value})
            beans.append({
                "id": row.get("ID"),
                "requestElementId": row.get("REQUEST_ELEMENT_ID"),
                "eventTypeCode": row.get("REGISTER_POINT_EVENT_TYPE_CODE"),
                "commentId": row.get("WEB_METER_READING_COMMENT_ID"),
                "meterReadingValue": value,
                "fileId": row.get("FILE_ID"),
            })
            info += (f"Счётчик №{serial},Марка ПУ:{row.get('METER_TYPE_ABBR')},"
                     f"Предыдущие показания:{row.get('PREV_METER_READING_VALUE')},"
                     f"Текущие показания:{value},Тип воды:{row.get('WATER_TYPE_NAME')}\n")
        unknown = set(values) - {r.get("PRODUCTION_TAG") for r in rows}
        if unknown:
            problems.append("счётчиков нет в договоре: " + ", ".join(sorted(unknown)))
        if not rows:
            problems.append("в заявке нет счётчиков")
        if problems:
            out["error"] = "; ".join(problems)
            return out
        body = {"accountId": account_id, "skipValidation": False,
                "meterReadDataPartBeans": beans, "infoData": info}
        if dry_run:
            return out
        res = await self._call("NaturalPersonMeterRead/send", json_body=body)
        out["response"] = res
        out["ok"] = isinstance(res, dict) and res.get("requestStatus") in ("POSTED", "REGISTERED")
        if not out["ok"]:
            out["error"] = "кабинет не принял показания: " + json.dumps(res, ensure_ascii=False)[:300]
        return out
