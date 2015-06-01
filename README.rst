*******************
OAuth-Client-Bridge
*******************

.. image:: https://img.shields.io/pypi/v/OAuth-Client-Bridge.svg?style=flat
    :target: https://pypi.python.org/pypi/OAuth-Client-Bridge/
    :alt: Latest PyPI version

.. image:: https://img.shields.io/pypi/dm/OAuth-Client-Bridge.svg?style=flat
    :target: https://pypi.python.org/pypi/OAuth-Client-Bridge/
    :alt: Number of PyPI downloads

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

Initialize the database::

    OAUTH_SETTINGS=config.py python
    >>> import oauthclientbridge
    >>> oauthclientbridge.init_db()

Run the development server::

    OAUTH_SETTINGS=config.py python -m oauthclientbridge

For further details on deploying Flask applications see the upstream
documentation.

Configuration
=============

Create a copy of ``sample_config.py`` and change all the settings as suggested
in the file. Make sure the environment the application runs in has
``OAUTH_SETTINGS`` pointing to the config file to use.

OAuth flows
===========

1. User gets sent to the landing page of the bridge.

2. An "Authorization Code Grant" flow takes place per
   `RFC6749 section 4.1<(https://tools.ietf.org/html/rfc6749#section-4.1>`_.

3. The bridge callback page generates a ``client_id`` / ``client_secret`` pair
   which is used to store the encrypted the authorization grant result.

4. The credentials are given to the client for use in the native application.

5. The application uses the bridge ``/token`` endpoint to do a "Client
   Credentials Grant" flow per
   `RFC6749 section 4.4<(https://tools.ietf.org/html/rfc6749#section-4.4>`_.

6. Our bridge fetches and decrypts previous token, refreshing if a refresh
   token was present, stores any new tokens and returns the grants.

Security
========

Note that there hasn't been any in depth analysis of this scheme. The following
notes are simply present to provide some insight into the choices that have
been made.

- Grants from the upstream provider are always encrypted with the client key
  which we don't store to minimize impact of unauthorized access to our database.

- The cryptography in question is Fernet from cryptography.io which gives us both
  signed and encrypted data so we can know if a valid secret was provided without
  storing the secret.

- Access to the ``/token`` endpoint is rate limited by both ``client_id`` and
  the remote address to slow down brute force attempts.

- The ``/revoke`` endpoint allows for deletion of the grants we've stored,
  invalidating a ``client_id``. This was added as we don't ever want to have to
  revoke the upstream secrets to reset access.

- If someone steals the client credentials all bets are off. Users should login
  to the upstream providers and revoke access. And also use our ``/revoke``
  endpoint.
