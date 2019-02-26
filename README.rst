*******************
OAuth-Client-Bridge
*******************

The OAuth2 Client Bridge provides a service to "convert" Authorization Code
Grants to Clients Grants suitable for use in "native applications" where it is
impractical to use Authorization Code grants directly.

Background
==========

This code exists to solve the "problem" of trying to authenticate native
applications against OAuth providers that only allow for long term grants via
Authorization Code Grants without giving the secrets to the native application.

After quite some back an forth this solution was devised as it felt the least
hackish, and somewhat cleanly combines two existing flows instead of creating
a strange hybrid flow.

Installation
============

Install by running::

    pip install OAuth-Client-Bridge


See ``oauthclientbridge/default_settings.py`` for details about the
configuration options. A minimal setup should set ``SECRET_KEY``,
``OAUTH_DATABASE``, ``OAUTH_CLIENT_ID``, ``OAUTH_CLIENT_SECRET``,
``OAUTH_AUTHORIZATION_URI`` and ``OAUTH_TOKEN_URI``.

Once this is done you can point ``OAUTH_SETTINGS`` at your new configuration
file and initialize the database::

    FLASK_APP=oauthclientbridge OAUTH_SETTINGS=oauth.cfg flask initdb

Run the development server::

    FLASK_APP=oauthclientbridge OAUTH_SETTINGS=oauth.cfg flask run

Additionally you might want to run ``cleandb`` as a cron job to clear out stale
data every now and then.::

    FLASK_APP=oauthclientbridge OAUTH_SETTINGS=oauth.cfg flask cleandb

Setting up a production instance
================================

- Always use HTTPS since we are passing access tokens around.
- Set ``SESSION_COOKIE_SECURE`` to keep the state used in the redirect safe.
- Ideally also set ``SESSION_COOKIE_DOMAIN`` and ``SESSION_COOKIE_PATH``.
- If you are behind a proxy set ``OAUTH_NUM_PROXIES`` to the number of proxies.
  This ensures ``X-Forwarded-For`` gets respected with the value from the proxy.

For further details on deploying Flask applications see the `upstream
documentation <http://flask.pocoo.org/docs/latest/deploying/>`_.

The following code snippet can be used to create a popup pointed at the oauth
server, and the poll the it for the results::

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

To get the snippet above to work setup the bridge with the following template
which will listen for the ``postMessage`` and then respond with the results.::

  OAUTH_CALLBACK_TEMPLATE = """
  <!DOCTYPE html>
  <html>
    <head>
      <meta charset="UTF-8">
      <title>Connect to example.</title>
      <script>
        var sourceOrigin = 'https://example.com:8080';

        window.addEventListener('message', function(event) {
          if (event.origin !== sourceOrigin) return;
          if (event.data !== 'oauthclientbridge') return;

          event.source.postMessage({
            client_id: {{ client_id|tojson }},
            client_secret: {{ client_secret|tojson }},
            error: {{ error|tojson }}
          }, event.origin);
        }, false)
      </script>
    </head>
    <body>
      <p>This popup should automatically close in a few seconds.</p>
    </body>
  </html>
  """


OAuth flows
===========

1. User gets sent to the landing page of the bridge.

2. An "Authorization Code Grant" flow takes place per
   `RFC6749 section 4.1 <https://tools.ietf.org/html/rfc6749#section-4.1>`_.

3. The bridge callback page generates a ``client_id`` / ``client_secret`` pair
   which is used to store the encrypted the authorization grant result.

4. The credentials are given to the client for use in the native application.

5. The application uses the bridge ``/token`` endpoint to do a "Client
   Credentials Grant" flow per
   `RFC6749 section 4.4 <https://tools.ietf.org/html/rfc6749#section-4.4>`_.

6. Our bridge fetches and decrypts previous token, refreshing if a refresh
   token was present, stores any new tokens and returns the grants.

Security
========

Note that there hasn't been any in depth analysis of this scheme. The following
notes are simply present to provide some insight into the choices that have
been made.

- Grants from the upstream provider are always encrypted with the client key
  which we don't store to minimize impact of unauthorized access to our database.

- The cryptography in question is Fernet from `cryptography.io
  <https://cryptography.io>`_ which gives us both signed and encrypted data so
  we can know if a valid secret was provided without storing the secret.

- Access to the endpoints should be rate limited in e.g. nginx.

- There is no endpoint for revoking credentials, it's expected the upstream
  provider will provide this for end-users.

- If someone steals the user's client credentials all bets are off. Users can
  login to the upstream provider and revoke access.
