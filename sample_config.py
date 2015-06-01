# Secret key used for encrypting session cookies used in initial OAuth flow:
SECRET_KEY = 'MUST-BE-REPLACED-WITH-A-GOOD-RANDOM-VALUE'

# Sqlite3 database to store tokens and rate limit information in:
OAUTH_DATABASE = 'oauth.db'

# Client ID and secret provided by upstream OAuth provider:
OAUTH_CLIENT_ID = 'some-client-id'
OAUTH_CLIENT_SECRET = 'some-client-secret'

# Upstream authorization URI to redirect users to:
OAUTH_AUTHORIZATION_URI= 'https://example.com/authorize'

# Upstream token and refresh URIs, this will often be the same endpoint:
OAUTH_TOKEN_URI = 'https://example.com/token'
OAUTH_REFRESH_URI = 'https://example.com/token'

# List of OAuth scopes to request from the upstream provider:
OAUTH_SCOPES = []

# Bridge callback URI to send users back to. Should exactly match URI
# registered with the upstream provider.
OAUTH_REDIRECT_URI = 'http://localhost:5000/callback'

# Jinja2 template to use for the callback page. Possible context values are:
#  error, client_id, client_secret
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
{% endif %}
"""
# Steady state QPS the rate limiter will allow:
OAUTH_BUCKET_REFILL_RATE = 2

# Maximum number of requests the rate limiter will allow in an initial burst:
OAUTH_BUCKET_CAPACITY = 5

# Upper limit on how many exceeding requests you will be penalized for:
OAUTH_BUCKET_MAX_HITS = 10
