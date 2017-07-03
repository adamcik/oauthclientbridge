from flask import jsonify, render_template_string, request, session

from oauthclientbridge import app, crypto, db, oauth, rate_limit, stats

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

    retry_after = rate_limit.check(request.remote_addr)
    if retry_after > 0:
        app.logger.warning('Rate limiting authorize: %s try again in %.2f',
                           request.remote_addr, retry_after)
        return _rate_limit(retry_after)

    default_scope = ' '.join(app.config['OAUTH_SCOPES'])
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

    retry_after = rate_limit.check(request.remote_addr)
    if retry_after > 0:
        app.logger.warning('Rate limiting callback: %s try again in %.2f',
                           request.remote_addr, retry_after)
        return _rate_limit(retry_after)


    error = None
    if session.get('state', object()) != request.args.get('state'):
        error = 'invalid_state'
    elif 'error' in request.args:
        error = request.args['error']

        if error == 'access_denied':
            app.logger.info('Resource owner denied the request.')
        elif error == 'invalid_scope':
            app.logger.warning('Invalid scope: %r', request.args.get('scope'))
        elif error not in oauth.ERROR_TYPES:
            app.logger.error('Invalid error: %s', error)
            error = 'invalid_error'
        else:
            app.logger.error('Callback failed: %s', error)

    if error is not None:
        # Treat error from query string as a client error.
        labels = {'method': request.method, 'url': request.base_url,
                  'status': stats.status_enum(400), 'error': error}
        stats.ClientErrorCounter.labels(**labels).inc()
        # TODO: Add human readable error?
        return _render(error=error), 400

    del session['state']  # Delete the state in case of replay.

    result = oauth.fetch(app.config['OAUTH_TOKEN_URI'],
                         app.config['OAUTH_CLIENT_ID'],
                         app.config['OAUTH_CLIENT_SECRET'],
                         grant_type='authorization_code',
                         redirect_uri=app.config['OAUTH_REDIRECT_URI'],
                         code=request.args.get('code'))

    if 'error' in result:
        app.logger.warning('Retrieving token failed: %s', result)
        # TODO: check error against error types, add human readable bit?
        return _render(error=result['error']), 400

    client_id = db.generate_id()
    client_secret = crypto.generate_key()
    token = crypto.dumps(client_secret, result)

    with db.cursor(name='insert_token') as cursor:
        try:
            cursor.execute(
                'INSERT INTO tokens (client_id, token) VALUES (?, ?)',
                (client_id, token))
        except db.IntegrityError:
            stats.DBErrorCounter.labels(
                query='insert_token', error='integrity_error').inc()
            app.log.warning('Could not get unique client id: %s', client_id)
            return _render(error='Database integrity error.'), 500

    return _render(client_id=client_id, client_secret=client_secret)


@app.route('/token', methods=['POST'])
def token():
    """Validate token request, refreshing when needed."""
    retry_after = rate_limit.check(request.remote_addr)
    if retry_after > 0:
        app.logger.warning('Rate limiting token: %s try again in %.2f',
                           request.remote_addr, retry_after)
        raise oauth.Error('invalid_request', 'Too many requests.',
                          retry_after=retry_after)

    if request.form.get('grant_type') != 'client_credentials':
        raise oauth.Error('unsupported_grant_type',
                          'Only "client_credentials" is supported.')
    elif request.form.get('scope'):
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

    if client_id:
        retry_after = rate_limit.check(client_id)
        if retry_after > 0:
            app.logger.warning('Rate limiting token: %s try again in %.2f',
                               client_id, retry_after)
            raise oauth.Error('invalid_request', 'Too many requests.',
                              retry_after=retry_after)

    if not client_id or not client_secret:
        raise oauth.Error('invalid_client',
                          'Both client_id and client_secret must be set.')

    with db.cursor(name='select_token') as cursor:
        cursor.execute('SELECT token FROM tokens WHERE client_id = ?',
                       (client_id,))
        row = cursor.fetchone()

    if row is None:
        raise oauth.Error('invalid_client', 'Client not known.')
    elif row[0] is None:
        # TODO: How do we avoid client retries here?
        raise oauth.Error('invalid_grant', 'Grant has been revoked.')

    try:
        result = crypto.loads(client_secret, row[0])
    except (crypto.InvalidToken, TypeError, ValueError):
        # Always return same message as for client not found to avoid leaking
        # valid clients directly, timing attacks could of course still work.
        raise oauth.Error('invalid_client', 'Client not known.')

    if 'refresh_token' not in result:
        return jsonify(result)

    refresh_result = oauth.fetch(
        app.config['OAUTH_REFRESH_URI'] or
        app.config['OAUTH_TOKEN_URI'],
        app.config['OAUTH_CLIENT_ID'],
        app.config['OAUTH_CLIENT_SECRET'],
        grant_type=app.config['OAUTH_GRANT_TYPE'],
        refresh_token=result['refresh_token'])

    if 'error' in refresh_result:
        # TODO: Consider deleting token when we get invalid_grant?

        # Log errors that aren't from revoked grants.
        if refresh_result['error'] != 'invalid_grant':
            app.logger.error('Token refresh failed: %s', refresh_result)

        # Client Credentials access token responses use the same errors
        # as Authorization Code Grant access token responses. As such just
        # raise the error we got.
        raise oauth.Error(refresh_result['error'],
                          refresh_result.get('error_description'),
                          refresh_result.get('error_uri'))

    result.update(refresh_result)
    token = crypto.dumps(client_secret, result)

    with db.cursor(name='update_token') as cursor:
        cursor.execute('UPDATE tokens SET token = ? WHERE client_id = ?',
                       (token, client_id))

    del result['refresh_token']
    return jsonify(result)


@app.route('/revoke', methods=['POST'])
def revoke():
    """Sets the clients token to null."""

    retry_after = rate_limit.check(request.remote_addr)
    if retry_after > 0:
        app.logger.warning('Rate limiting revoke: %s try again in %.2f',
                           request.remote_addr, retry_after)
        return _rate_limit(retry_after)
    elif 'client_id' not in request.form:
        # TODO: Register server error in this case.
        return _render(error='Missing client_id.'), 400

    with db.cursor(name='revoke_token') as cursor:
        cursor.execute('UPDATE tokens SET token = null WHERE client_id = ?',
                       (request.form['client_id'],))

    # We always report success as to not leak info.
    return _render(error='Revoked client_id.'), 200


@app.route('/metrics', methods=['GET'])
def metrics():
    return stats.export_metrics()


def _render(client_id=None, client_secret=None, error=None):
    return render_template_string(
        app.config['OAUTH_CALLBACK_TEMPLATE'],
        client_id=client_id, client_secret=client_secret, error=error)


def _rate_limit(retry_after):
        headers = [('Retry-After', str(int(retry_after + 1)))]
        return _render(error='Too many requests.'), 429, headers
