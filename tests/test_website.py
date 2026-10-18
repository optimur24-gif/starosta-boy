import asyncio

import httpx
import pytest

from bgmu_bot.source import SourceError
from bgmu_bot.website import WebsiteClient, validate_proxy_url


@pytest.mark.asyncio
async def test_website_requests_are_fresh_and_preserve_parameters():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, text=f"version {len(requests)}")

    async with WebsiteClient(transport=httpx.MockTransport(respond)) as client:
        first = await client.get("/", params={"date": "2026-10-21"})
        second = await client.get("/", params={"date": "2026-10-21"})
    assert first.text == "version 1"
    assert second.text == "version 2"
    assert requests[0].url.params["date"] == "2026-10-21"
    assert all(request.url.host == "bgmu-time.ru" for request in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["https://example.com/", "http://bgmu-time.ru/", "//example.com/", "https://user:password@bgmu-time.ru/"])
async def test_transport_rejects_other_origins_and_credentials(path):
    async with WebsiteClient(transport=httpx.MockTransport(lambda request: pytest.fail("Unexpected request"))) as client:
        with pytest.raises(ValueError, match="Разрешены запросы"):
            await client.get(path)


@pytest.mark.asyncio
async def test_upstream_http_failure_is_not_a_blank_schedule():
    async with WebsiteClient(transport=httpx.MockTransport(lambda request: httpx.Response(503))) as client:
        with pytest.raises(SourceError, match="HTTP 503"):
            await client.get()


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [httpx.ProxyError, httpx.ConnectTimeout, httpx.ConnectError])
async def test_connection_errors_do_not_expose_proxy_credentials(error_type):
    def fail(request):
        raise error_type("http://private-user:private-password@proxy.example:3128", request=request)

    async with WebsiteClient(transport=httpx.MockTransport(fail)) as client:
        with pytest.raises(SourceError) as caught:
            await client.get()
    assert "private-user" not in str(caught.value)
    assert "private-password" not in str(caught.value)


@pytest.mark.parametrize("proxy", [
    "http://user:password@proxy.example:3128",
    "https://proxy.example:443",
    "socks5://user:password@proxy.example:1080",
    "socks5h://proxy.example:1080",
])
def test_supported_proxy_protocols(proxy):
    assert validate_proxy_url(proxy) == proxy


@pytest.mark.parametrize("proxy", [
    "proxy.example:3128", "ftp://proxy.example:21", "http://private-password@proxy.example:bad",
    "http://proxy.example:3128/unexpected", "http://proxy.example", "http://:3128",
])
def test_invalid_proxy_configuration_has_safe_error(proxy):
    with pytest.raises(ValueError) as caught:
        validate_proxy_url(proxy)
    assert "private-password" not in str(caught.value)


def test_environment_proxy_is_not_set_globally(monkeypatch):
    captured = {}
    monkeypatch.setenv("BGMU_PROXY_URL", "http://user:password@proxy.example:3128")
    monkeypatch.setenv("HTTPS_PROXY", "http://existing-platform-proxy.example:80")

    def make_client(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(httpx, "AsyncClient", make_client)
    client = WebsiteClient.from_environment()
    assert client.uses_proxy
    assert captured["proxy"] == "http://user:password@proxy.example:3128"
    assert captured["trust_env"] is True
    import os
    assert os.environ["HTTPS_PROXY"] == "http://existing-platform-proxy.example:80"


@pytest.mark.asyncio
async def test_https_website_really_uses_proxy_connect():
    received = []

    async def proxy_connection(reader, writer):
        try:
            received.append(await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3))
            writer.write(b"HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(proxy_connection, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server, WebsiteClient(f"http://test-user:test-password@127.0.0.1:{port}") as client:
        with pytest.raises(SourceError, match="прокси отклонил"):
            await client.get()
    assert len(received) == 1
    assert received[0].startswith(b"CONNECT bgmu-time.ru:443 HTTP/1.1\r\n")
    assert b"Proxy-Authorization: Basic " in received[0]


@pytest.mark.asyncio
async def test_socks_dependency_is_available():
    async with WebsiteClient("socks5://127.0.0.1:1080") as client:
        assert client.uses_proxy
