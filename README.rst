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
hackish, and somewhat cleanly combines to existing flows instead of creating
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
- Note that this app does not handle proxying. See `flask proxy setups
  <http://flask.pocoo.org/docs/latest/deploying/wsgi-standalone/#proxy-setups>`_
  for create WSGI shim that fixes this. Note that uWSGI+NGINX does not have
  this problem and is the recommended deployment method.

.. TODO: Add notes about OAUTH_CALLBACK_TEMPLATE setup?

For further details on deploying Flask applications see the `upstream
documentation <http://flask.pocoo.org/docs/latest/deploying/>`_.

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

- Access to the ``/token`` endpoint is rate limited by both ``client_id`` and
  the remote address to slow down brute force attempts.

- All other endpoints are rate limited by the remote address only.

- The ``/revoke`` endpoint allows for deletion of the grants we've stored,
  invalidating a ``client_id``. This was added as we don't ever want to have to
  revoke the upstream secrets to reset access.

- If someone steals the client credentials all bets are off. Users can either
  login to the upstream provider and revoke access. Or use our ``/revoke``
  endpoint.
