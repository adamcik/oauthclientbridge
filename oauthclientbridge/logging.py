import logging
import logging.handlers
import typing

from flask import request

from oauthclientbridge import app

if typing.TYPE_CHECKING:
    from typing import Any, List, Text, Tuple, Union  # noqa: F401


class ContextualFilter(logging.Filter):
    def filter(self, record):  # type: (Any) -> bool
        record.request_path = request.path
        record.request_base_url = request.base_url
        record.request_method = request.method
        record.request_remote_address = request.remote_addr
        return True


class CustomSMTPHandler(logging.handlers.SMTPHandler):
    def __init__(self, mailhost, fromaddr, toaddrs, subject):
        # type: (Union[str, Tuple[str, int]], str, List[str], str) -> None
        super().__init__(mailhost, fromaddr, toaddrs, subject)
        self.subject_formatter = logging.Formatter(subject)

    def getSubject(self, record):  # noqa: N802
        # type: (logging.LogRecord) -> str
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
