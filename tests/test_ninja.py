import httpx
import pytest

from sniper.ninja import BACKOFF_BASE_S, NinjaBackoff, NinjaClient


def client_with(handler) -> tuple[NinjaClient, list]:
    calls: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    transport = httpx.MockTransport(wrapped)
    http = httpx.AsyncClient(transport=transport, base_url="https://poe.ninja")
    return NinjaClient(client=http), calls


async def test_etag_conditional_requests():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("If-None-Match") == 'W/"abc"':
            return httpx.Response(304)
        return httpx.Response(200, json={"lines": [1]}, headers={"ETag": 'W/"abc"'})

    client, calls = client_with(handler)
    first = await client.get_json("/poe1/api/economy/leagues")
    second = await client.get_json("/poe1/api/economy/leagues")
    assert first == second == {"lines": [1]}
    assert len(calls) == 2
    assert calls[1].headers["If-None-Match"] == 'W/"abc"'


async def test_429_enters_backoff_and_skips_network():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "120"})

    client, calls = client_with(handler)
    with pytest.raises(NinjaBackoff):
        await client.get_json("/x")
    assert client.in_backoff
    assert client.backoff_remaining > BACKOFF_BASE_S  # Retry-After=120 respected

    # second call is refused locally without touching the network
    with pytest.raises(NinjaBackoff):
        await client.get_json("/x")
    assert len(calls) == 1


async def test_5xx_backoff_grows_then_success_resets():
    fail = {"on": True}

    def handler(request: httpx.Request) -> httpx.Response:
        if fail["on"]:
            return httpx.Response(503)
        return httpx.Response(200, json=[{"id": "Allflame"}])

    client, _ = client_with(handler)
    with pytest.raises(NinjaBackoff):
        await client.get_json("/x")
    assert client.in_backoff
    client._next_allowed = 0.0  # simulate the wait elapsing
    fail["on"] = False
    assert await client.current_league() == "Allflame"
    assert not client.in_backoff
    assert client._fail_count == 0
