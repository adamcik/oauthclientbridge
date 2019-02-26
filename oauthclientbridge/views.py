from __future__ import absolute_import

import logging

from flask import jsonify, render_template_string, request, session

from oauthclientbridge import app, crypto, db, errors, oauth, stats

# Disable caching across the board.
app.after_request(oauth.nocache)

# Handle OAuth and unhandled errors automatically.
app.register_error_handler(oauth.Error, oauth.error_handler)
app.register_error_handler(500, oauth.fallback_error_handler)

# Keep track of requests and response stats.
app.before_request(stats.before_request)
app.after_request(stats.after_request)


@app.route('/')
def authorize():
    """Store random state in session cookie and redirect to auth endpoint."""

    default_scope = ' '.join(app.config['OAUTH_SCOPES'] or [])
    session['state'] = crypto.generate_key()

    return oauth.redirect(
        app.config['OAUTH_AUTHORIZATION_URI'],
        client_id=app.config['OAUTH_CLIENT_ID'],
        response_type='code',
        redirect_uri=app.config['OAUTH_REDIRECT_URI'],
        scope=request.args.get('scope', default_scope),
        state=session['state'])


@app.route('/callback')
def callback():
    """Validate callback and trade in code for a token."""
    error, desc = None, None

    if session.pop('state', object()) != request.args.get('state'):
        error = errors.INVALID_STATE
        desc = 'Client state does not match callback state, possible replay.'
    elif 'error' in request.args:
        error = request.args['error']
        error = oauth.normalize_error(error, oauth.AUTHORIZATION_ERRORS)
        desc = errors.ERROR_DESCRIPTIONS[error]
    elif not request.args.get('code'):
        error = errors.INVALID_REQUEST
        desc = 'Authorization code missing from provider callback.'

    if error is not None:
        if error == errors.INVALID_SCOPE:
            scope = request.args.get('scope')
            _log(error, 'Callback failed %s: %s - %r', error, desc, scope)
        else:
            _log(error, 'Callback failed %s: %s', error, desc)
        return _error(error, desc)

    result = oauth.fetch(app.config['OAUTH_TOKEN_URI'],
                         app.config['OAUTH_CLIENT_ID'],
                         app.config['OAUTH_CLIENT_SECRET'],
                         grant_type='authorization_code',
                         redirect_uri=app.config['OAUTH_REDIRECT_URI'],
                         code=request.args.get('code'), endpoint='token')

    if 'error' in result:
        error = oauth.normalize_error(result['error'], oauth.TOKEN_ERRORS)
        desc = errors.ERROR_DESCRIPTIONS[error]
    elif not oauth.validate_token(result):
        error = 'invalid_response'
        desc = 'Invalid response from provider.'

    if error is not None:
        app.logger.warning('Retrieving token failed: %s', result)
        return _error(error, desc)

    if 'refresh_token' in result:
        result = oauth.scrub_refresh_token(result)

    client_secret = crypto.generate_key()
    token = crypto.dumps(client_secret, result)

    try:
        client_id = db.insert(token)
    except db.IntegrityError:
        app.log.warning('Could not get unique client id: %s', client_id)
        return _error('integrity_error', 'Database integrity error.')

    return _render(client_id=client_id, client_secret=client_secret)


@app.route('/token', methods=['POST'])
def token():
    """Validate token request, refreshing when needed."""
    # TODO: allow all methods and raise invalid_request for !POST?

    if request.form.get('grant_type') != 'client_credentials':
        raise oauth.Error(errors.UNSUPPORTED_GRANT_TYPE,
                          'Only "client_credentials" is supported.')
    elif 'scope' in request.form:
        raise oauth.Error(errors.INVALID_SCOPE,
                          'Setting scope is not supported.')
    elif request.authorization and request.authorization.type != 'basic':
        raise oauth.Error(errors.INVALID_CLIENT,
                          'Only Basic Auth is supported.')

    client_id = request.form.get('client_id')
    client_secret = request.form.get('client_secret')
    if (client_id or client_secret) and request.authorization:
        raise oauth.Error(errors.INVALID_REQUEST,
                          'More than one mechanism for authenticating set.')
    elif request.authorization:
        client_id = request.authorization.username
        client_secret = request.authorization.password

    if not client_id or not client_secret:
        raise oauth.Error(errors.INVALID_CLIENT,
                          'Both client_id and client_secret must be set.')
    elif client_id == client_secret:
        raise oauth.Error(errors.INVALID_CLIENT,
                          'client_id and client_secret set to same value.')

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
        return jsonify(result)

    refresh_result = oauth.fetch(
        app.config['OAUTH_REFRESH_URI'] or app.config['OAUTH_TOKEN_URI'],
        app.config['OAUTH_CLIENT_ID'],
        app.config['OAUTH_CLIENT_SECRET'],
        grant_type=app.config['OAUTH_GRANT_TYPE'],
        refresh_token=result['refresh_token'], endpoint='refresh')

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
        raise oauth.Error(error,
                          refresh_result.get('error_description'),
                          refresh_result.get('error_uri'))

    if not oauth.validate_token(refresh_result):
        raise oauth.Error(errors.INVALID_REQUEST,
                          'Invalid response from provider.')

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
        db.update(client_id, crypto.dumps(client_secret, modified))

    # Only return what we got from the API (minus refresh_token).
    return jsonify(refresh_result)


@app.route('/metrics', methods=['GET'])
def metrics():
    try:
        active, revoked = db.tokens()
    except db.Error:
        pass
    else:
        stats.TokenGauge.labels(state='revoked').set(revoked)
        stats.TokenGauge.labels(state='active').set(active)

    return stats.export_metrics()


def _log(error, msg, *args, **kwargs):
    level = app.config['OAUTH_ERROR_LOG_LEVELS'].get(error, 'ERROR')
    level = logging.getLevelName(level)
    app.logger.log(level, msg, *args, **kwargs)


def _error(error_code, error):
    if error_code == errors.INVALID_CLIENT:
        status = 401
    else:
        status = 400

    stats.ServerErrorCounter.labels(
        endpoint=stats.endpoint(), status=stats.status(status),
        error=error_code).inc()

    return _render(error=error_code, description=error), status


def _render(client_id=None, client_secret=None, error=None, description=None):
    # Keep all the vars in something we can dump for tests with tojson.
    variables = {'client_id': client_id, 'client_secret': client_secret,
                 'error': error, 'description': description}
    return render_template_string(app.config['OAUTH_CALLBACK_TEMPLATE'],
                                  variables=variables, **variables)
