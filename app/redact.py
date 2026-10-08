"""Keep the Exotel token out of logs.

uvicorn logs every WebSocket handshake at INFO on "uvicorn.error" with the
full path and query string ('"WebSocket /ws/exotel/<token>" [accepted]'),
and HTTP requests on "uvicorn.access". This filter masks the token in both
URL forms before anything is written.
"""

import logging
import re

_PATTERNS = (
    (re.compile(r"(/ws/exotel/)[^/?\s\"']+"), r"\1***"),
    (re.compile(r"([?&]token=)[^&\s\"']*"), r"\1***"),
)


def redact(text: str) -> str:
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


class RedactTokenFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(redact(a) if isinstance(a, str) else a for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: redact(v) if isinstance(v, str) else v for k, v in record.args.items()}
        return True


def redact_uvicorn_logs() -> None:
    for name in ("uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        if not any(isinstance(f, RedactTokenFilter) for f in logger.filters):
            logger.addFilter(RedactTokenFilter())
