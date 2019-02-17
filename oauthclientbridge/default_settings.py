# Secret key used for encrypting session cookies used in initial OAuth flow,
# MUST be set.
#
# Run the following code in a Python shell and then copy the entire value as is
# into you config file for a good secret key.
#
#   >>> import os
#   >>> os.urandom(24)
#   '\xfd{H\xe5<\x95\xf9\xe3\x96.5\xd1\x01O<!\xd5\xa2\xa0\x9fR"\xa1\xa8'
#
# Also make sure you have secure access to this file _and_ the directory it's
# in to keep it from leaking via .pyc files.
SECRET_KEY = None

# SQLite3 database to store tokens information in, MUST be set.
OAUTH_DATABASE = None

# SQlite3 database timeout to use at "connection" time.
OAUTH_DATABASE_TIMEOUT = 5

# SQlite3 database PRAGMA to run at connection time for database.
OAUTH_DATABASE_PRAGMA = 'PRAGMA journal_mode = WAL'

# Client ID and secret provided by upstream OAuth provider, MUST be set.
OAUTH_CLIENT_ID = None
OAUTH_CLIENT_SECRET = None

# Type of grant to request from upstream.
OAUTH_GRANT_TYPE = 'refresh_token'

# List of OAuth scopes to request from the upstream provider:
OAUTH_SCOPES = []

# Upstream authorization URI to redirect users to, MUST be set.
OAUTH_AUTHORIZATION_URI = None

# Upstream token and refresh URIs. The token URI MUST be set, while the refresh
# one will fallback to the token URI.
OAUTH_TOKEN_URI = None
OAUTH_REFRESH_URI = None

# Overall allowed timeout across all retires, backoff and retry-after time.
OAUTH_FETCH_TOTAL_TIMEOUT = 20

# Number of seconds to wait for initial connection and subsequent reads to
# upstream OAuth endpoint for a single fetch attempt.
OAUTH_FETCH_TIMEOUT = 5.0

# Maximum number of retries for fetching oauth data.
OAUTH_FETCH_TOTAL_RETRIES = 3

# Status codes that should be considered retryable for oauth.
OAUTH_FETCH_RETRY_STATUS_CODES = (429, 500, 502, 503, 504)

# Status codes to treat as temporarily_unavailable when we can't decode the
# repsonse. Remaining status codes treated as server_error.
OAUTH_FETCH_UNAVAILABLE_STATUS_CODES = (429, 502, 503, 504)

# Non-standard oauth errors and what standard errors to translate them to.
OAUTH_FETCH_ERROR_TYPES = {'errorTransient': 'temporarily_unavailable'}

# Backoff factor to use for not hammering the oauth server too much.
OAUTH_FETCH_BACKOFF_FACTOR = 0.1

# Bridge callback URI to send users back to. Should exactly match URI
# registered with the upstream provider.
OAUTH_REDIRECT_URI = 'http://localhost:5000/callback'

# Jinja2 template to use for the callback page. Possible context values are:
#  error, description, client_id, client_secret
#
# Should be setup to give the client_id and client_secret to the user. Either
# directly or passing the value back to the parent window if this is being run
# in a pop-up window.
OAUTH_CALLBACK_TEMPLATE = """
{% if error %}
  {{ error }}{% if description %}: {{ description }}{% endif %}
{% else %}
  <form action="token" method="POST">
    Client ID: <input name="client_id" value="{{ client_id }}" />
    Client Secret: <input name="client_secret" value="{{ client_secret }}" />
    Grant type: <input name="grant_type" value="client_credentials" />
    <button>Fetch token</button>
  </form>
  <form action="revoke" method="POST">
    Client ID: <input name="client_id" value="{{ client_id }}" />
    <button>Revoke token</button>
  </form>
{% endif %}
"""

# Number proxies to expect in front of us. Used for handling X-Forwarded-For
OAUTH_NUM_PROXIES = 0

# Additional log file for application level logging, set to None to disable.
OAUTH_LOG_FILE = None

# Log level for file logging.
OAUTH_LOG_FILE_LEVEL = 'INFO'

# Log format for file logging.
OAUTH_LOG_FILE_FORMAT = ('%(asctime)s %(levelname)s: %(message)s '
                         '[in %(pathname)s:%(lineno)d]')

# Max bytes to pass to the RotatingFileHandler logging handler.
OAUTH_LOG_FILE_MAX_BYTES = 0

# Number of backups that the RotatingFileHandler should keep.
OAUTH_LOG_FILE_BACKUP_COUNT = 0

# List of addresses to send logging emails to, leave empty to disable.
OAUTH_LOG_EMAIL = []

# Log level for email logging.
OAUTH_LOG_EMAIL_LEVEL = 'ERROR'

# Log format for email logging.
OAUTH_LOG_EMAIL_FORMAT = """
%(message)s

Remote address:   %(request_remote_address)s
Time:             %(asctime)s
Message type:     %(levelname)s
Path:             %(request_path)s
Location:         %(pathname)s:%(lineno)d
Module:           %(module)s
Function:         %(funcName)s
"""

# SMTP host to use for email logging.
OAUTH_LOG_EMAIL_HOST = 'localhost'

# From address to user for email logging.
OAUTH_LOG_EMAIL_FROM = 'oauthclientbridge@localhost'

# Subject line to use for email logging.
OAUTH_LOG_EMAIL_SUBJECT = 'oauthclientbridge: %(request_base_url)s'

# Log levels to use for errors in callback flow.
OAUTH_ERROR_LOG_LEVELS = {
    'access_denied': 'INFO',
    'invalid_state': 'WARNING',
    'temporarily_unavailable': 'INFO',
}
