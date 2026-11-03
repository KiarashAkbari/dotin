"""HTTP layer: GET and POST with retry, backoff and defined failure behaviour."""
from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import requests

from .config import HttpConfig
from .errors import FetchError

MAX_RETRY_AFTER_SECONDS = 60.0


def _parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        current = now or datetime.now(timezone.utc)
        return max(0.0, (when - current).total_seconds())
    return seconds if seconds >= 0 else None


class HttpClient:
    def __init__(
        self,
        config: HttpConfig,
        session: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._config = config
        self._session = session if session is not None else requests.Session()
        self._sleep = sleep
        self.request_count = 0

    def get_json(self, url: str, params: dict | None = None,
                 headers: dict | None = None) -> list | dict:
        return self.request_json("GET", url, params=params, headers=headers)

    def post_json(self, url: str, body: dict,
                  headers: dict | None = None) -> list | dict:
        return self.request_json("POST", url, json_body=body, headers=headers)

    def request_json(self, method: str, url: str, *, params: dict | None = None,
                     json_body: dict | None = None,
                     headers: dict | None = None) -> list | dict:
        attempts = self._config.max_retries + 1
        reason = "unknown"
        for attempt in range(attempts):
            retry_after = None
            self.request_count += 1
            try:
                response = self._session.request(
                    method, url, params=params, json=json_body, headers=headers,
                    timeout=self._config.timeout_seconds,
                )
            except requests.RequestException as exc:
                reason = f"transport error: {type(exc).__name__}"
            else:
                status = response.status_code
                if status == 200:
                    try:
                        payload = response.json()
                    except ValueError:
                        reason = "invalid or truncated JSON body"
                    else:
                        if not isinstance(payload, (list, dict)):
                            raise FetchError(
                                f"Unexpected JSON type {type(payload).__name__}",
                                context={"url": url, "method": method},
                            )
                        return payload
                elif status in self._config.retry_statuses:
                    reason = f"HTTP {status}"
                    retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                else:
                    raise FetchError(
                        f"HTTP {status} from {url}",
                        context={"url": url, "method": method, "status": status},
                    )
            if attempt < attempts - 1:
                self._sleep(self._delay(attempt, retry_after))
        raise FetchError(
            f"Giving up after {attempts} attempts: {reason}",
            context={"url": url, "method": method, "attempts": attempts},
        )

    def _delay(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return min(retry_after, MAX_RETRY_AFTER_SECONDS)
        return self._config.backoff_base_seconds * (self._config.backoff_factor ** attempt)