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

# SQLite3 database to store tokens and rate limit information in, MUST be set.
OAUTH_DATABASE = None

# Client ID and secret provided by upstream OAuth provider, MUST be set.
OAUTH_CLIENT_ID = None
OAUTH_CLIENT_SECRET = None

# List of OAuth scopes to request from the upstream provider:
OAUTH_SCOPES = []

# Upstream authorization URI to redirect users to, MUST be set.
OAUTH_AUTHORIZATION_URI = None

# Upstream token and refresh URIs. The token URI MUST be set, while the refresh
# one will fallback to the token URI.
OAUTH_TOKEN_URI = None
OAUTH_REFRESH_URI = None

# Bridge callback URI to send users back to. Should exactly match URI
# registered with the upstream provider.
OAUTH_REDIRECT_URI = 'http://localhost:5000/callback'

# Jinja2 template to use for the callback page. Possible context values are:
#  error, client_id, client_secret
#
# Should be setup to give the client_id and client_secret to the user. Either
# directly or passing the value back to the parent window if this is being run
# in a pop-up window.
OAUTH_CALLBACK_TEMPLATE = """
{% if error %}
  {{ error }}
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

# If we should rate limit calls to the bridge.
OAUTH_RATE_LIMIT = True

# Steady state QPS the rate limiter will allow. This controls how quickly our
# tracking buckets empty, i.e. the QPS.
OAUTH_BUCKET_REFILL_RATE = 2

# Maximum number of requests the rate limiter will allow in an initial burst:
OAUTH_BUCKET_CAPACITY = 10

# Upper limit on how full the bucket can get. This ensures that you don't lock
# yourself out for to long if you do a lot of excessive requests.
OAUTH_BUCKET_MAX_HITS = 15
