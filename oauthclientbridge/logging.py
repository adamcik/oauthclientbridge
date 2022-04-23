import logging
import logging.handlers
from typing import Any

from flask import request

from oauthclientbridge import app


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


context_provider = ContextualFilter()
app.logger.addFilter(context_provider)
app.logger.setLevel("DEBUG")

if app.config["OAUTH_LOG_FILE"]:
    file_handler = logging.handlers.RotatingFileHandler(
        app.config["OAUTH_LOG_FILE"],
        maxBytes=app.config["OAUTH_LOG_FILE_MAX_BYTES"],
        backupCount=app.config["OAUTH_LOG_FILE_BACKUP_COUNT"],
    )
    file_handler.setFormatter(logging.Formatter(app.config["OAUTH_LOG_FILE_FORMAT"]))
    file_handler.setLevel(app.config["OAUTH_LOG_FILE_LEVEL"])
    app.logger.addHandler(file_handler)


if not app.debug and app.config["OAUTH_LOG_EMAIL"]:
    mail_handler = CustomSMTPHandler(
        app.config["OAUTH_LOG_EMAIL_HOST"],
        app.config["OAUTH_LOG_EMAIL_FROM"],
        app.config["OAUTH_LOG_EMAIL"],
        app.config["OAUTH_LOG_EMAIL_SUBJECT"],
    )
    mail_handler.setFormatter(logging.Formatter(app.config["OAUTH_LOG_EMAIL_FORMAT"]))
    mail_handler.setLevel(app.config["OAUTH_LOG_EMAIL_LEVEL"])
    app.logger.addHandler(mail_handler)
