import unittest

import requests

from data_factory.config import HttpConfig
from data_factory.errors import FetchError
from data_factory.http import HttpClient

BAD_JSON = object()


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        if self._payload is BAD_JSON:
            raise ValueError("bad json")
        return self._payload


class FakeSession:
    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def make_client(outcomes, retries=3, factor=2.0):
    session = FakeSession(outcomes)
    sleeps = []
    config = HttpConfig(
        timeout_seconds=5.0,
        max_retries=retries,
        backoff_base_seconds=2.0,
        backoff_factor=factor,
        retry_statuses=frozenset({429, 500, 503}),
    )
    return HttpClient(config, session=session, sleep=sleeps.append), session, sleeps


class HttpClientTests(unittest.TestCase):
    def test_get_returns_payload(self):
        client, session, _ = make_client([FakeResponse(200, [{"a": 1}])])
        result = client.get_json("https://x.test", params={"$limit": 1})
        self.assertEqual(result, [{"a": 1}])
        method, _, kwargs = session.calls[0]
        self.assertEqual(method, "GET")
        self.assertEqual(kwargs["params"], {"$limit": 1})

    def test_post_sends_json_body(self):
        client, session, _ = make_client([FakeResponse(200, {"ok": True})])
        result = client.post_json("https://x.test", {"query": "select 1"})
        self.assertEqual(result, {"ok": True})
        method, _, kwargs = session.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(kwargs["json"], {"query": "select 1"})

    def test_retries_on_503_then_succeeds(self):
        client, session, sleeps = make_client(
            [FakeResponse(503), FakeResponse(503), FakeResponse(200, [])]
        )
        self.assertEqual(client.get_json("https://x.test"), [])
        self.assertEqual(len(session.calls), 3)
        self.assertEqual(sleeps, [2.0, 4.0])
        self.assertEqual(client.request_count, 3)

    def test_client_error_not_retried(self):
        client, session, sleeps = make_client([FakeResponse(404)])
        with self.assertRaises(FetchError) as ctx:
            client.get_json("https://x.test")
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(sleeps, [])
        self.assertEqual(ctx.exception.context["status"], 404)

    def test_status_outside_retry_list_not_retried(self):
        client, session, sleeps = make_client([FakeResponse(501)])
        with self.assertRaises(FetchError) as ctx:
            client.get_json("https://x.test")
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(sleeps, [])
        self.assertEqual(ctx.exception.context["status"], 501)

    def test_gives_up_after_max_retries(self):
        client, session, sleeps = make_client([FakeResponse(500)] * 3, retries=2)
        with self.assertRaises(FetchError) as ctx:
            client.get_json("https://x.test")
        self.assertEqual(len(session.calls), 3)
        self.assertEqual(sleeps, [2.0, 4.0])
        self.assertEqual(ctx.exception.context["attempts"], 3)

    def test_backoff_factor_comes_from_config(self):
        client, _, sleeps = make_client(
            [FakeResponse(503), FakeResponse(503), FakeResponse(200, [])], factor=3.0
        )
        client.get_json("https://x.test")
        self.assertEqual(sleeps, [2.0, 6.0])

    def test_truncated_json_is_retried(self):
        client, session, _ = make_client(
            [FakeResponse(200, BAD_JSON), FakeResponse(200, [{"id": 1}])]
        )
        self.assertEqual(client.get_json("https://x.test"), [{"id": 1}])
        self.assertEqual(len(session.calls), 2)

    def test_transport_error_is_retried(self):
        client, session, _ = make_client(
            [requests.ConnectionError("down"), FakeResponse(200, [])]
        )
        self.assertEqual(client.get_json("https://x.test"), [])
        self.assertEqual(len(session.calls), 2)

    def test_retry_after_header_is_honoured(self):
        client, _, sleeps = make_client(
            [FakeResponse(429, headers={"Retry-After": "7"}), FakeResponse(200, [])]
        )
        client.get_json("https://x.test")
        self.assertEqual(sleeps, [7.0])

    def test_unexpected_json_type_fails_fast(self):
        client, session, _ = make_client([FakeResponse(200, "oops")])
        with self.assertRaises(FetchError):
            client.get_json("https://x.test")
        self.assertEqual(len(session.calls), 1)


if __name__ == "__main__":
    unittest.main()