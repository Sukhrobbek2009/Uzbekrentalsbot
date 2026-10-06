import logging
import re

# Telegram API URLs embed the token as /bot<id>:<secret>/method.
_URL_TOKEN = re.compile(r"bot\d+:[\w-]+")
# Vatan Rentals access/refresh tokens are JWTs.
_JWT = re.compile(r"eyJ[\w-]+\.[\w-]+\.[\w-]+")


class RedactingFormatter(logging.Formatter):
    """Strips the bot token from every log line, including tracebacks."""

    def __init__(self, fmt: str, *secrets: str | None) -> None:
        super().__init__(fmt)
        self._secrets = [x for x in secrets if x]

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for secret in self._secrets:
            text = text.replace(secret, "<redacted>")
        text = _JWT.sub("<jwt-redacted>", text)
        return _URL_TOKEN.sub("bot<redacted>", text)


def setup_logging(*secrets: str | None) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        RedactingFormatter("%(asctime)s %(name)s %(levelname)s %(message)s", *secrets)
    )
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    # httpx logs full request URLs (which contain the token) at INFO.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
