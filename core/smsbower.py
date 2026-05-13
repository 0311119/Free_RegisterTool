"""SMS activation client (smsbower-compatible API shape).

Supports multiple providers that expose the same `handler_api.php` contract:
- smsbower
- 5sim (compat endpoint)
- herosms
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Callable

import requests


BASE_URL = "https://smsbower.page/stubs/handler_api.php"
PROVIDER_BASE_URLS = {
    "smsbower": BASE_URL,
    "5sim": "https://api1.5sim.net/stubs/handler_api.php",
    "herosms": "https://hero-sms.com/stubs/handler_api.php",
}
DEFAULT_TIMEOUT = 300
DEFAULT_POLL_INTERVAL = 5.0


@dataclass
class SmsBowerNumber:
    activation_id: str
    phone_number: str
    service: str
    country: str
    quality: str = ""  # gold / silver / "" = any


class SmsBowerError(RuntimeError):
    pass


class SmsBowerRequestError(SmsBowerError):
    pass


class SmsBowerBalanceError(SmsBowerError):
    pass


class SmsBowerNoNumberError(SmsBowerError):
    pass


class SmsBowerInvalidPhoneExceptionError(SmsBowerError):
    pass


class SmsBowerTimeoutError(SmsBowerError):
    pass


class SmsBowerWaitRetryError(SmsBowerError):
    pass


def _call(
    api_key: str,
    params: dict,
    timeout: float = 30,
    *,
    base_url: str = BASE_URL,
    provider_label: str = "SMSBOWER",
) -> str:
    payload = dict(params)
    payload["api_key"] = api_key
    try:
        response = requests.get(base_url, params=payload, timeout=timeout)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise SmsBowerRequestError(f"{provider_label} request failed: {exc}") from exc

    text = response.text.strip()
    if text.startswith("BAD_KEY"):
        raise SmsBowerError(f"{provider_label} API key invalid")
    if text.startswith("BAD_ACTION"):
        raise SmsBowerError(f"{provider_label} unknown action: {payload.get('action', '?')}")
    if text.startswith("NO_BALANCE"):
        raise SmsBowerBalanceError(f"{provider_label} balance too low")
    if text.startswith("NO_NUMBERS"):
        raise SmsBowerNoNumberError(f"{provider_label} no numbers available")
    if text.startswith("ERROR_"):
        raise SmsBowerError(f"{provider_label} error: {text}")
    return text


class SmsBowerClient:
    """Client for smsbower-compatible handler API."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = BASE_URL,
        provider_label: str = "SMSBOWER",
    ):
        if not api_key:
            raise SmsBowerError(f"{provider_label} API key missing")
        self.api_key = api_key
        self.base_url = base_url
        self.provider_label = provider_label

    @classmethod
    def from_provider(cls, provider: str, api_key: str, base_url: str | None = None) -> "SmsBowerClient":
        normalized = str(provider or "smsbower").strip().lower()
        resolved_base_url = base_url or PROVIDER_BASE_URLS.get(normalized) or BASE_URL
        label = normalized.upper()
        return cls(api_key, base_url=resolved_base_url, provider_label=label)

    def get_balance(self) -> float:
        text = _call(
            self.api_key,
            {"action": "getBalance"},
            base_url=self.base_url,
            provider_label=self.provider_label,
        )
        match = re.search(r"ACCESS_BALANCE:([\d.]+)", text)
        if match:
            return float(match.group(1))
        raise SmsBowerError(f"balance parse failed: {text}")

    def get_number(
        self,
        service: str,
        country: str = "6",
        max_price: float | None = None,
        min_price: float | None = None,
        operator: str | None = None,
        phone_exception: str | None = None,
        provider_ids: str | None = None,
        except_provider_ids: str | None = None,
        quality: str = "",
    ) -> SmsBowerNumber:
        params: dict = {"action": "getNumber", "service": service, "country": country}
        if max_price is not None:
            params["maxPrice"] = str(max_price)
        if min_price is not None:
            params["minPrice"] = str(min_price)
        if operator:
            params["operator"] = operator
        if phone_exception:
            params["phoneException"] = phone_exception
        if provider_ids:
            params["providerIds"] = provider_ids
        if except_provider_ids:
            params["exceptProviderIds"] = except_provider_ids
        if quality and quality.lower() in ("gold", "silver"):
            params["type"] = quality.lower()

        text = _call(
            self.api_key,
            params,
            base_url=self.base_url,
            provider_label=self.provider_label,
        )
        if text.startswith("WRONG_EXCEPTION_PHONE"):
            raise SmsBowerInvalidPhoneExceptionError(text)
        match = re.match(r"ACCESS_NUMBER:(\d+):(\d+)", text)
        if match:
            return SmsBowerNumber(
                activation_id=match.group(1),
                phone_number=match.group(2),
                service=service,
                country=country,
                quality=quality,
            )
        raise SmsBowerError(f"number parse failed: {text}")

    def get_status(self, activation_id: str) -> tuple[str, str | None]:
        text = _call(
            self.api_key,
            {"action": "getStatus", "id": activation_id},
            base_url=self.base_url,
            provider_label=self.provider_label,
        )
        if text.startswith("STATUS_OK:"):
            return ("ok", text.split(":", 1)[1].strip())
        if text == "STATUS_WAIT_CODE":
            return ("wait", None)
        if text == "STATUS_WAIT_RETRY":
            return ("retry", None)
        if text == "STATUS_CANCEL":
            return ("cancel", None)
        raise SmsBowerError(f"unknown status: {text}")

    def set_status(self, activation_id: str, status: int) -> str:
        return _call(
            self.api_key,
            {"action": "setStatus", "id": activation_id, "status": str(status)},
            base_url=self.base_url,
            provider_label=self.provider_label,
        )

    def wait_for_code(
        self,
        activation_id: str,
        timeout: int = DEFAULT_TIMEOUT,
        interval: float = DEFAULT_POLL_INTERVAL,
        on_poll: Callable[[str, str | None], None] | None = None,
    ) -> str:
        deadline = time.monotonic() + timeout
        transient_errors = 0
        while time.monotonic() < deadline:
            try:
                status, code = self.get_status(activation_id)
            except SmsBowerRequestError as exc:
                transient_errors += 1
                if on_poll:
                    on_poll("request_error", None)
                if transient_errors >= 12:
                    raise SmsBowerTimeoutError(
                        f"poll failed by network errors {transient_errors} times: {exc}"
                    )
                time.sleep(min(interval, max(1.0, deadline - time.monotonic())))
                continue
            except SmsBowerError as exc:
                transient_errors += 1
                if on_poll:
                    on_poll(f"provider_error:{exc}", None)
                if transient_errors >= 12:
                    raise SmsBowerTimeoutError(
                        f"poll failed by provider errors {transient_errors} times: {exc}"
                    )
                time.sleep(min(interval, max(1.0, deadline - time.monotonic())))
                continue
            if on_poll:
                on_poll(status, code)
            if status == "ok" and code:
                try:
                    self.set_status(activation_id, 6)
                except Exception:
                    pass
                return code
            if status == "retry":
                raise SmsBowerWaitRetryError("provider requested retry")
            if status == "cancel":
                raise SmsBowerError("activation canceled")
            time.sleep(min(interval, max(0, deadline - time.monotonic())))
        raise SmsBowerTimeoutError(f"wait code timeout ({timeout}s)")

    def cancel(self, activation_id: str) -> None:
        try:
            self.set_status(activation_id, 8)
        except Exception:
            pass

    def get_prices(self, service: str, country: str) -> dict:
        text = _call(
            self.api_key,
            {"action": "getPrices", "service": service, "country": country},
            base_url=self.base_url,
            provider_label=self.provider_label,
        )
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {}
