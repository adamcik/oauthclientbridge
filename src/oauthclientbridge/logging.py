import logging
import logging.handlers
from typing import Any

from flask import request

from oauthclientbridge import get_settings


class ContextualFilter(logging.Filter):
    def filter(self, record: Any) -> bool:
        record.request_path = request.path
        record.request_base_url = request.base_url
        record.request_method = request.method
        record.request_remote_address = request.remote_addr
        return True


class CustomSMTPHandler(logging.handlers.SMTPHandler):
    def __init__(self, mailhost: str, fromaddr: str, toaddrs: list[str], subject: str):
        super().__init__(mailhost, fromaddr, toaddrs, subject)
        self.subject_formatter = logging.Formatter(subject)

    def getSubject(self, record: logging.LogRecord) -> str:
        return self.subject_formatter.format(record).split("\n")[0]


def configure() -> None:
    settings = get_settings()

    context_provider = ContextualFilter()
    logging.getLogger().addFilter(context_provider)
    logging.getLogger().setLevel("DEBUG")

    if settings.logging.file:
        file_handler = logging.handlers.RotatingFileHandler(
            settings.logging.file,
            maxBytes=settings.logging.file_max_bytes,
            backupCount=settings.logging.file_backup_count,
        )
        file_handler.setFormatter(logging.Formatter(settings.logging.file_format))
        file_handler.setLevel(settings.logging.file_level)
        logging.getLogger().addHandler(file_handler)

    if settings.logging.email:
        mail_handler = CustomSMTPHandler(
            settings.logging.email_host,
            settings.logging.email_from,
            settings.logging.email,
            settings.logging.email_subject,
        )
        mail_handler.setFormatter(logging.Formatter(settings.logging.email_format))
        mail_handler.setLevel(settings.logging.email_level)
        logging.getLogger().addHandler(mail_handler)
