"""HTTP transport for the schedule website, separate from Telegram traffic."""

import os
from urllib.parse import urlsplit

import httpx

from .source import SourceError


SITE_URL = "https://bgmu-time.ru/"


def validate_proxy_url(value: str | None) -> str | None:
    if not value or not value.strip():
        return None
    value = value.strip()
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme in {"http", "https", "socks5", "socks5h"}
            and bool(parsed.hostname)
            and parsed.port is not None
            and parsed.path in {"", "/"}
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        # Never include the supplied value: it may contain a login/password.
        raise ValueError(
            "Некорректный BGMU_PROXY_URL. Нужен URL вида "
            "http://LOGIN:PASSWORD@HOST:PORT или socks5://LOGIN:PASSWORD@HOST:PORT."
        ) from None
    return value


class WebsiteClient:
    def __init__(
        self,
        proxy_url: str | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        proxy_url = validate_proxy_url(proxy_url)
        self.uses_proxy = proxy_url is not None
        self._client = httpx.AsyncClient(
            base_url=SITE_URL,
            proxy=proxy_url,
            transport=transport,
            # Keep the platform's TLS trust and network configuration when an
            # explicit deployment proxy has not been selected.
            trust_env=True,
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
            max_redirects=5,
            headers={"User-Agent": "BGMU-Schedule-Bot/0.1"},
        )

    @classmethod
    def from_environment(cls) -> "WebsiteClient":
        return cls(os.environ.get("BGMU_PROXY_URL"))

    async def get(self, path: str = "/", *, params=None) -> httpx.Response:
        url = httpx.URL(SITE_URL).join(path)
        if url.scheme != "https" or url.host != "bgmu-time.ru" or url.port not in {None, 443} or url.userinfo:
            raise ValueError("Разрешены запросы только к https://bgmu-time.ru/.")
        try:
            response = await self._client.get(url, params=params)
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as error:
            raise SourceError(f"Сайт вернул HTTP {error.response.status_code}.") from None
        except httpx.ProxyError:
            raise SourceError(
                "Сетевой шлюз или прокси отклонил соединение. "
                "Проверьте доступ, порт и авторизацию прокси."
            ) from None
        except httpx.TimeoutException:
            raise SourceError("Истекло время ожидания ответа сайта или прокси.") from None
        except httpx.RequestError:
            raise SourceError("Не удалось установить защищённое соединение с сайтом.") from None

    async def aclose(self):
        await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        await self.aclose()
