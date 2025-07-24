# OAuth-Client-Bridge

The OAuth2 Client Bridge provides a service to "convert" Authorization
Code Grants to Clients Grants suitable for use in "native applications"
where it is impractical to use Authorization Code grants directly.

## Background

This code exists to solve the "problem" of trying to authenticate native
applications against OAuth providers that only allow for long term
grants via Authorization Code Grants without giving the secrets to the
native application.

After quite some back an forth this solution was devised as it felt the
least hackish, and somewhat cleanly combines two existing flows instead
of creating a strange hybrid flow.

## Installation

Install by running:

    pip install OAuth-Client-Bridge

Settings are managed by Pydantic and loaded from environment variables. Each setting is prefixed based on its category (e.g., `OAUTH_`, `DB_`, `BRIDGE_`, `FETCH_`). Refer to the `Settings` class in `src/oauthclientbridge/settings.py` for all available options and their prefixes. A minimal setup should define the following environment variables (see `.env.example` for a complete list):

-   `BRIDGE_SECRET_KEY`: Secret key used for encrypting session cookies.
-   `DB_DATABASE`: SQLite3 database path.
-   `OAUTH_CLIENT_ID`: Client ID from your OAuth provider.
-   `OAUTH_CLIENT_SECRET`: Client secret from your OAuth provider.
-   `OAUTH_AUTHORIZATION_URI`: Upstream authorization URI.
-   `OAUTH_TOKEN_URI`: Upstream token URI.

Once these are set (e.g., by sourcing a `.env` file), you can initialize the database:

    FLASK_APP=oauthclientbridge flask initdb

Run the development server:

    FLASK_APP=oauthclientbridge flask run

Additionally you might want to run `cleandb` as a cron job to clear out
stale data every now and then.:

    FLASK_APP=oauthclientbridge flask cleandb

## Setting up a production instance

-   Always use HTTPS since we are passing access tokens around.
-   Set `SESSION_COOKIE_SECURE` to keep the state used in the redirect
    safe.
-   Ideally also set `SESSION_COOKIE_DOMAIN` and `SESSION_COOKIE_PATH`.
-   If you are behind a proxy set `BRIDGE_NUM_PROXIES` to the number of
    proxies. This ensures `X-Forwarded-For` gets respected with the
    value from the proxy.

For further details on deploying Flask applications see the [upstream
documentation][].

The following code snippet can be used to create a popup pointed at the
oauth server, and the poll the it for the results:

    var target = 'https://example.net/oauth';
    var targetOrigin = 'https://example.net'

    window.addEventListener('message', function(event) {
      if (event.origin !== targetOrigin) return;

      if (event.data['error']) {
        // Update webpage with error data.
      } else {
        // Update webpage with client_id and client_secret.
      }

      event.source.close();
    }, false);

    var child = window.open(target);
    var interval = setInterval(function() {
      if (child.closed) {
        clearInterval(interval);
      } else {
        child.postMessage('oauthclientbridge', targetOrigin)
      }
    }, 1000);

To get the snippet above to work setup the bridge with the following
template which will listen for the `postMessage` and then respond with
the results.:

        BRIDGE_CALLBACK_TEMPLATE

  [upstream documentation]: http://flask.pocoo.org/docs/latest/deploying/
