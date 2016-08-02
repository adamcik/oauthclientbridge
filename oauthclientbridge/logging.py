from __future__ import absolute_import

from logging import Formatter
from logging.handlers import RotatingFileHandler, SMTPHandler

from oauthclientbridge import app


class CustomSMTPHandler(SMTPHandler):
    def getSubject(self, record):  # noqa: N802
        return self.subject.format(record)


if app.config['OAUTH_LOG_FILE']:
    file_handler = RotatingFileHandler(
        app.config['OAUTH_LOG_FILE'],
        maxBytes=app.config['OAUTH_LOG_FILE_MAX_BYTES'],
        backupCount=app.config['OAUTH_LOG_FILE_BACKUP_COUNT'])
    file_handler.setFormatter(Formatter(app.config['OAUTH_LOG_FILE_FORMAT']))
    file_handler.setLevel(app.config['OAUTH_LOG_FILE_LEVEL'])
    app.logger.addHandler(file_handler)


# TODO: Add SMTPHandler sub-class that uses a formatter for the subject?
if not app.debug and app.config['OAUTH_LOG_EMAIL']:
    subject_formatter = Formatter(app.config['OAUTH_LOG_EMAIL_SUBJECT'])
    mail_handler = CustomSMTPHandler(app.config['OAUTH_LOG_EMAIL_HOST'],
                                     app.config['OAUTH_LOG_EMAIL_FROM'],
                                     app.config['OAUTH_LOG_EMAIL'],
                                     subject_formatter)
    mail_handler.setFormatter(Formatter(app.config['OAUTH_LOG_EMAIL_FORMAT']))
    mail_handler.setLevel(app.config['OAUTH_LOG_EMAIL_LEVEL'])
    app.logger.addHandler(mail_handler)
