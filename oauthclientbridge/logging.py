from __future__ import absolute_import

from logging import Filter, Formatter
from logging.handlers import RotatingFileHandler, SMTPHandler

from flask import request

from oauthclientbridge import app


class ContextualFilter(Filter):
    def filter(self, log_record):
        log_record.request_path = request.path
        log_record.request_base_url = request.base_url
        log_record.request_method = request.method
        log_record.request_remote_address = request.remote_addr
        return True


class CustomSMTPHandler(SMTPHandler):
    def __init__(self, mailhost, fromaddr, toaddrs, subject):
        super(CustomSMTPHandler, self).__init__(
            mailhost, fromaddr, toaddrs, subject
        )
        self.subject_formatter = Formatter(subject)

    def getSubject(self, record):  # noqa: N802
        return self.subject_formatter.format(record).replace('\n', '')


context_provider = ContextualFilter()
app.logger.addFilter(context_provider)
app.logger.setLevel('DEBUG')

if app.config['OAUTH_LOG_FILE']:
    file_handler = RotatingFileHandler(
        app.config['OAUTH_LOG_FILE'],
        maxBytes=app.config['OAUTH_LOG_FILE_MAX_BYTES'],
        backupCount=app.config['OAUTH_LOG_FILE_BACKUP_COUNT'],
    )
    file_handler.setFormatter(Formatter(app.config['OAUTH_LOG_FILE_FORMAT']))
    file_handler.setLevel(app.config['OAUTH_LOG_FILE_LEVEL'])
    app.logger.addHandler(file_handler)


if not app.debug and app.config['OAUTH_LOG_EMAIL']:
    mail_handler = CustomSMTPHandler(
        app.config['OAUTH_LOG_EMAIL_HOST'],
        app.config['OAUTH_LOG_EMAIL_FROM'],
        app.config['OAUTH_LOG_EMAIL'],
        app.config['OAUTH_LOG_EMAIL_SUBJECT'],
    )
    mail_handler.setFormatter(Formatter(app.config['OAUTH_LOG_EMAIL_FORMAT']))
    mail_handler.setLevel(app.config['OAUTH_LOG_EMAIL_LEVEL'])
    app.logger.addHandler(mail_handler)
