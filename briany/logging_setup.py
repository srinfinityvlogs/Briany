"""Logging that never prints secret values, even by accident."""
import logging


class RedactFilter(logging.Filter):
    def __init__(self, secrets):
        super().__init__()
        self._secrets = [s for s in secrets if s and len(s) >= 6]

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for s in self._secrets:
            msg = msg.replace(s, "***")
        record.msg, record.args = msg, ()
        return True


def setup_logging(level: str = "INFO", secrets=()) -> logging.Logger:
    root = logging.getLogger()
    root.setLevel(level)
    if not root.handlers:
        root.addHandler(logging.StreamHandler())
    for h in root.handlers:
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        h.addFilter(RedactFilter(secrets))
    return logging.getLogger("briany")
