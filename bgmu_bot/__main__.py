import logging
import asyncio
import os
import sys
from urllib.parse import unquote, urlsplit

from .bgmu import BGMUSource
from .ui import create_application
from .website import WebsiteClient


class RedactingFormatter(logging.Formatter):
    def __init__(self, sensitive_values):
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")
        self.sensitive_values = sorted({value for value in sensitive_values if value}, key=len, reverse=True)

    def format(self, record):
        text = super().format(record)
        for value in self.sensitive_values:
            text = text.replace(value, "[REDACTED]")
        return text


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("Задайте TELEGRAM_BOT_TOKEN в защищённых настройках окружения хостинга.", file=sys.stderr)
        return 1
    proxy = os.environ.get("BGMU_PROXY_URL", "")
    sensitive = [token, proxy]
    try:
        parsed = urlsplit(proxy)
        sensitive.extend(unquote(value) for value in (parsed.username, parsed.password) if value)
    except ValueError:
        pass
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter(sensitive))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    try:
        if os.environ.get('RESTRICTED_BOT_TOKEN', '').strip():
            from .runner import run_together
            asyncio.run(run_together(token))
            return 0
        website = WebsiteClient.from_environment()
        application = create_application(token, BGMUSource(website))
        application.run_polling(bootstrap_retries=0, drop_pending_updates=False)
    except Exception as error:
        logging.getLogger(__name__).error("Запуск бота завершился ошибкой (%s). Проверьте токен и подключение.", type(error).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
