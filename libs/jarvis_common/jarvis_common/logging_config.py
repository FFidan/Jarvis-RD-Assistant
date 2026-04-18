"""Structured JSON logging for JARVIS services."""

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime

request_id_ctx: ContextVar[str] = ContextVar("request_id", default="")


class JSONFormatter(logging.Formatter):
    """Format log records as single-line JSON.

    Parameters
    ----------
    service_name : str
        Name included in every log entry for service identification.
    """

    def __init__(self, service_name: str) -> None:
        super().__init__()
        self.service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": self.service_name,
            "request_id": request_id_ctx.get(""),
        }
        if record.exc_info and record.exc_info[0] is not None:
            log_entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_entry)


def configure_logging(service_name: str, log_level: str = "INFO") -> None:
    """Replace default logging config with structured JSON output.

    Parameters
    ----------
    service_name : str
        Service identifier included in every log line.
    log_level : str
        Root logger level (default ``"INFO"``).
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter(service_name))
    root.addHandler(handler)
    # Quiet noisy third-party loggers
    for noisy in ("httpx", "httpcore", "asyncio", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
