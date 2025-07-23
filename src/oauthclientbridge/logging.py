import logging
import logging.handlers
from typing import Any

from flask import request

from oauthclientbridge import get_settings

settings = get_settings()


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
    context_provider = ContextualFilter()
    logging.getLogger().addFilter(context_provider)
    logging.getLogger().setLevel("DEBUG")

    if settings.log_file:
        file_handler = logging.handlers.RotatingFileHandler(
            settings.log_file,
            maxBytes=settings.log_file_max_bytes,
            backupCount=settings.log_file_backup_count,
        )
        file_handler.setFormatter(logging.Formatter(settings.log_file_format))
        file_handler.setLevel(settings.log_file_level)
        logging.getLogger().addHandler(file_handler)

    if settings.log_email:
        mail_handler = CustomSMTPHandler(
            settings.log_email_host,
            settings.log_email_from,
            settings.log_email,
            settings.log_email_subject,
        )
        mail_handler.setFormatter(logging.Formatter(settings.log_email_format))
        mail_handler.setLevel(settings.log_email_level)
        logging.getLogger().addHandler(mail_handler)
