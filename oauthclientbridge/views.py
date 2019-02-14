from flask import jsonify, render_template_string, request, session

from oauthclientbridge import app, crypto, db, oauth, stats

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

    error = None

    # TODO: switch to pop for getting state so it always gets cleared?
    if session.pop('state', object()) != request.args.get('state'):
        error = 'invalid_state'
    elif 'error' in request.args:
        error = oauth.normalize_error(request.args['error'])

        # TODO: Probably not worth it sanity checking the error enum, the state
        # check would filter out anyone passing in random things trivially.
        if error == 'access_denied':
            app.logger.info('Resource owner denied the request.')
        elif error == 'invalid_scope':
            app.logger.warning('Invalid scope: %r', request.args.get('scope'))
        elif error == 'invalid_error':
            app.logger.error('Invalid error: %s', request.args['error'])
        else:
            # TODO: Reduce this to warning for temporarily_unavailable?
            app.logger.error('Callback failed: %s', error)
    elif not request.args.get('code'):
        error = 'invalid_request'

    if error is not None:
        # TODO: Add human readable error to pass to the template?
        return _error(error, error, 400)

    result = oauth.fetch(app.config['OAUTH_TOKEN_URI'],
                         app.config['OAUTH_CLIENT_ID'],
                         app.config['OAUTH_CLIENT_SECRET'],
                         grant_type='authorization_code',
                         redirect_uri=app.config['OAUTH_REDIRECT_URI'],
                         code=request.args.get('code'), endpoint='token')

    if 'error' in result:
        app.logger.warning('Retrieving token failed: %s', result)
        # TODO: Add human readable error to pass to the template?
        error = oauth.normalize_error(result['error'])
        return _error(error, error, 400)

    if not result.get('access_token') or not result.get('token_type'):
        description = 'Provider response missing required entries.'
        return _error(description, 'server_error', 400)

    client_secret = crypto.generate_key()
    token = crypto.dumps(client_secret, result)

    try:
        client_id = db.insert(token)
    except db.IntegrityError:
        app.log.warning('Could not get unique client id: %s', client_id)
        return _error('integrity_error', 'Database integrity error.', 500)

    return _render(client_id=client_id, client_secret=client_secret)


@app.route('/token', methods=['POST'])
def token():
    """Validate token request, refreshing when needed."""
    # TODO: allow all methods and raise invalid_request for !POST?

    if request.form.get('grant_type') != 'client_credentials':
        raise oauth.Error('unsupported_grant_type',
                          'Only "client_credentials" is supported.')
    elif 'scope' in request.form:
        raise oauth.Error('invalid_scope', 'Setting scope is not supported.')
    elif request.authorization and request.authorization.type != 'basic':
        raise oauth.Error('invalid_client', 'Only Basic Auth is supported.')

    client_id = request.form.get('client_id')
    client_secret = request.form.get('client_secret')
    if (client_id or client_secret) and request.authorization:
        raise oauth.Error('invalid_request',
                          'More than one mechanism for authenticating set.')
    elif request.authorization:
        client_id = request.authorization.username
        client_secret = request.authorization.password

    if not client_id or not client_secret:
        raise oauth.Error('invalid_client',
                          'Both client_id and client_secret must be set.')
    elif client_id == client_secret:
        raise oauth.Error('invalid_client',
                          'client_id and client_secret set to same value.')

    try:
        token = db.lookup(client_id)
    except LookupError:
        raise oauth.Error('invalid_client', 'Client not known.')

    if token is None:
        # TODO: How do we avoid client retries here?
        raise oauth.Error('invalid_grant', 'Grant has been revoked.')

    try:
        result = crypto.loads(client_secret, token)
    except (crypto.InvalidToken, TypeError, ValueError):
        # Always return same message as for client not found to avoid leaking
        # valid clients directly, timing attacks could of course still work.
        raise oauth.Error('invalid_client', 'Client not known.')

    if 'refresh_token' not in result:
        return jsonify(result)

    refresh_result = oauth.fetch(
        app.config['OAUTH_REFRESH_URI'] or app.config['OAUTH_TOKEN_URI'],
        app.config['OAUTH_CLIENT_ID'],
        app.config['OAUTH_CLIENT_SECRET'],
        grant_type=app.config['OAUTH_GRANT_TYPE'],
        refresh_token=result['refresh_token'], endpoint='refresh')

    if 'error' in refresh_result:
        error = oauth.normalize_error(refresh_result['error'])
        if error == 'invalid_grant':
            db.update(client_id, None)
            app.logger.warning('Revoked: %s', client_id)
        elif error == 'temporarily_unavailable':
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

    # TODO: Only update if refresh has new values (excluding access_token)?
    # TODO: Don't store access_token in DB?
    result.update(refresh_result)
    token = crypto.dumps(client_secret, result)

    db.update(client_id, token)

    del result['refresh_token']
    return jsonify(result)


# TODO: https://tools.ietf.org/html/rfc7009
@app.route('/revoke', methods=['POST'])
def revoke():
    """Sets the clients token to null."""

    if 'client_id' not in request.form:
        return _error('invalid_request', 'Missing client_id.', 400)

    db.update(request.form['client_id'], None)
    app.logger.warning('Revoked: %s', request.form['client_id'])

    # We always report success as to not leak info.
    return _render(error='Revoked client_id.'), 200


@app.route('/metrics', methods=['GET'])
def metrics():
    try:
        with db.cursor(name='metrics') as cursor:
            cursor.execute('SELECT COUNT(*), token IS NULL FROM tokens GROUP BY 2')
            rows = cursor.fetchall()
    except db.Error:
        pass
    else:
        for row in rows:
            if row[1]:
                stats.TokenGauge.labels(state='revoked').set(row[0])
            else:
                stats.TokenGauge.labels(state='active').set(row[0])

    return stats.export_metrics()


def _error(error_code, error, status):
    stats.ServerErrorCounter.labels(
        method=request.method, endpoint=stats.endpoint(),
        status=stats.status(status), error=error_code).inc()
    return _render(error=error), status


def _render(client_id=None, client_secret=None, error=None):
    return render_template_string(
        app.config['OAUTH_CALLBACK_TEMPLATE'],
        client_id=client_id, client_secret=client_secret, error=error)
