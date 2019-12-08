from __future__ import absolute_import

import logging
import typing

import flask

from oauthclientbridge import app, crypto, db, errors, oauth, stats

if typing.TYPE_CHECKING:
    from typing import Optional, Text  # noqa: F401


# Disable caching across the board.
app.after_request(oauth.nocache)

# Handle OAuth and unhandled errors automatically.
app.register_error_handler(oauth.Error, oauth.error_handler)
app.register_error_handler(500, oauth.fallback_error_handler)

# Keep track of requests and response stats.
app.before_request(stats.before_request)
app.after_request(stats.after_request)


@app.route('/')
def authorize():  # type: () -> flask.Response
    """Store random state in session cookie and redirect to auth endpoint."""

    redirect_uri = flask.request.args.get('redirect_uri')
    if redirect_uri and redirect_uri != app.config['OAUTH_REDIRECT_URI']:
        return _error(errors.INVALID_REQUEST, 'Wrong redirect_uri.')

    default_scope = ' '.join(app.config['OAUTH_SCOPES'] or [])

    flask.session['client_state'] = flask.request.args.get('state')
    flask.session['state'] = crypto.generate_key()

    return oauth.redirect(
        app.config['OAUTH_AUTHORIZATION_URI'],
        client_id=app.config['OAUTH_CLIENT_ID'],
        response_type='code',
        redirect_uri=app.config['OAUTH_REDIRECT_URI'],
        scope=flask.request.args.get('scope', default_scope),
        state=flask.session['state'],
    )


@app.route('/callback')
def callback():  # type: () -> flask.Response
    """Validate callback and trade in code for a token."""
    error = None  # type: Optional[Text]
    desc = None  # type: Optional[Text]
    client_state = flask.session.pop('client_state', None)
    state = flask.session.pop('state', None)

    if not flask.request.args:
        error = errors.INVALID_REQUEST
        desc = 'No arguments provided, request is invalid.'
    elif state is None:
        error = errors.INVALID_STATE
        desc = 'State is not set, this page was probably refreshed.'
    elif state != flask.request.args.get('state'):
        error = errors.INVALID_STATE
        desc = 'State does not match callback state.'
    elif 'error' in flask.request.args:
        error = flask.request.args['error']
        if error is not None:
            error = oauth.normalize_error(error, oauth.AUTHORIZATION_ERRORS)
            desc = errors.DESCRIPTIONS[error]
    elif not flask.request.args.get('code'):
        error = errors.INVALID_REQUEST
        desc = 'Authorization code missing from provider callback.'

    if error is not None:
        level = app.config['OAUTH_ERROR_LOG_LEVELS'].get(error, 'ERROR')
        level = logging.getLevelName(level)

        msg = 'Callback failed %s: %s' % (error, desc)
        if error == errors.INVALID_SCOPE:
            msg += ' - %r' % flask.request.args.get('scope')
        app.logger.log(level, msg)

        return _error(error, desc, client_state)

    result = oauth.fetch(
        app.config['OAUTH_TOKEN_URI'],
        app.config['OAUTH_CLIENT_ID'],
        app.config['OAUTH_CLIENT_SECRET'],
        grant_type='authorization_code',
        redirect_uri=app.config['OAUTH_REDIRECT_URI'],
        code=flask.request.args.get('code'),
        endpoint='token',
    )

    if 'error' in result:
        error = oauth.normalize_error(result['error'], oauth.TOKEN_ERRORS)
        desc = errors.DESCRIPTIONS[error]
    elif not oauth.validate_token(result):
        error = 'invalid_response'
        desc = 'Invalid response from provider.'

    if error is not None:
        app.logger.warning('Retrieving token failed: %s', result)
        return _error(error, desc, client_state)

    if 'refresh_token' in result:
        result = oauth.scrub_refresh_token(result)

    client_secret = crypto.generate_key()
    token = crypto.dumps(client_secret, result)

    try:
        client_id = db.insert(token)
    except db.IntegrityError:
        app.log.warning('Could not get unique client id.')
        return _error(
            'integrity_error', 'Database integrity error.', client_state
        )

    return _render(
        client_id=client_id, client_secret=client_secret, state=client_state
    )


@app.route('/token', methods=['POST'])
def token():  # type: () -> flask.Response
    """Validate token request, refreshing when needed."""
    # TODO: allow all methods and raise invalid_request for !POST?

    if flask.request.form.get('grant_type') != 'client_credentials':
        raise oauth.Error(
            errors.UNSUPPORTED_GRANT_TYPE,
            'Only "client_credentials" is supported.',
        )
    elif 'scope' in flask.request.form:
        raise oauth.Error(
            errors.INVALID_SCOPE, 'Setting scope is not supported.'
        )

    try:
        # Trigger decoding base64 value that might have bad Unicode data.
        authorization = flask.request.authorization
    except ValueError:
        authorization = None

    if authorization and authorization.type != 'basic':
        raise oauth.Error(
            errors.INVALID_CLIENT, 'Only Basic Auth is supported.'
        )

    client_id = flask.request.form.get('client_id')
    client_secret = flask.request.form.get('client_secret')
    if (client_id or client_secret) and authorization:
        raise oauth.Error(
            errors.INVALID_REQUEST,
            'More than one mechanism for authenticating set.',
        )
    elif authorization:
        client_id = authorization.username
        client_secret = authorization.password

    if not client_id or not client_secret:
        raise oauth.Error(
            errors.INVALID_CLIENT,
            'Both client_id and client_secret must be set.',
        )
    elif client_id == client_secret:
        raise oauth.Error(
            errors.INVALID_CLIENT,
            'client_id and client_secret set to same value.',
        )

    try:
        token = db.lookup(client_id)
    except LookupError:
        raise oauth.Error(errors.INVALID_CLIENT, 'Client not known.')

    if token is None:
        # TODO: How do we avoid client retries here?
        raise oauth.Error(errors.INVALID_GRANT, 'Grant has been revoked.')

    try:
        result = crypto.loads(client_secret, token)
    except (crypto.InvalidToken, TypeError, ValueError):
        # Always return same message as for client not found to avoid leaking
        # valid clients directly, timing attacks could of course still work.
        raise oauth.Error(errors.INVALID_CLIENT, 'Client not known.')

    if 'refresh_token' not in result:
        return flask.jsonify(result)

    refresh_result = oauth.fetch(
        app.config['OAUTH_REFRESH_URI'] or app.config['OAUTH_TOKEN_URI'],
        app.config['OAUTH_CLIENT_ID'],
        app.config['OAUTH_CLIENT_SECRET'],
        grant_type=app.config['OAUTH_GRANT_TYPE'],
        refresh_token=result['refresh_token'],
        endpoint='refresh',
    )

    if 'error' in refresh_result:
        error = refresh_result['error']
        error = oauth.normalize_error(error, oauth.TOKEN_ERRORS)

        if error == errors.INVALID_GRANT:
            db.update(client_id, None)
            app.logger.warning('Revoked: %s', client_id)
        elif error == errors.TEMPORARILY_UNAVAILABLE:
            app.logger.warning('Token refresh failed: %s', refresh_result)
        else:
            app.logger.error('Token refresh failed: %s', refresh_result)

        # Client Credentials access token responses use the same errors
        # as Authorization Code Grant access token responses. As such, just
        # raise the error we got.
        # TODO: Retry after header for error case?
        raise oauth.Error(
            error,
            refresh_result.get('error_description'),
            refresh_result.get('error_uri'),
        )

    if not oauth.validate_token(refresh_result):
        raise oauth.Error(
            errors.INVALID_REQUEST, 'Invalid response from provider.'
        )

    # Copy over original scope if not set in refresh.
    if 'scope' not in refresh_result and 'scope' in result:
        refresh_result['scope'] = result['scope']

    # Copy of stored db token to track if we need to update anything.
    modified = oauth.scrub_refresh_token(result)

    # Remove any new refresh_token and update DB with new value.
    if 'refresh_token' in refresh_result:
        modified['refresh_token'] = refresh_result['refresh_token']
        del refresh_result['refresh_token']

    # Reduce write pressure by only issuing update on changes.
    if result != modified:
        app.logger.warning('Updating token for: %s', client_id)
        db.update(client_id, crypto.dumps(client_secret, modified))

    # Only return what we got from the API (minus refresh_token).
    return flask.jsonify(refresh_result)


@app.route('/metrics', methods=['GET'])
def metrics():  # () -> flask.Response
    return stats.export_metrics()


def _error(error_code, error=None, state=None):
    # type: (Text, Optional[Text], Optional[Text]) -> flask.Response
    if error_code == errors.INVALID_CLIENT:
        status = 401
    else:
        status = 400

    stats.ServerErrorCounter.labels(
        endpoint=stats.endpoint(), status=stats.status(status), error=error_code
    ).inc()

    response = _render(error=error_code, description=error, state=state)
    response.status_code = status
    return response


def _render(
    client_id=None,  # type: Optional[Text]
    client_secret=None,  # type: Optional[Text]
    state=None,  # type: Optional[Text]
    error=None,  # type: Optional[Text]
    description=None,  # type: Optional[Text]
):  # type: (...) -> flask.Response
    # Keep all the vars in something we can dump for tests with tojson.
    variables = {
        'client_id': client_id,
        'client_secret': client_secret,
        'state': state,
        'error': error,
        'description': description,
    }
    return flask.Response(
        flask.render_template_string(
            app.config['OAUTH_CALLBACK_TEMPLATE'],
            variables=variables,
            **variables
        ).encode('utf-8'),
        content_type='text/html; charset=UTF-8',
    )
