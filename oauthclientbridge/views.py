from flask import jsonify, render_template_string, request, session

from oauthclientbridge import app, crypto, db, oauth, rate_limit

# Disable caching, and handle OAuth error responses automatically.
app.after_request(oauth.nocache)
app.register_error_handler(oauth.Error, oauth.error_handler)


@app.route('/')
def authorize():
    """Store random state in session cookie and redirect to auth endpoint."""

    if rate_limit.check(request.remote_addr):
        app.logger.warning('Rate limiting authorize.')
        return _render(error='Too many requests.'), 429

    session['state'] = crypto.generate_key()
    return oauth.redirect(
        app.config['OAUTH_AUTHORIZATION_URI'],
        client_id=app.config['OAUTH_CLIENT_ID'],
        response_type='code',
        redirect_uri=app.config['OAUTH_REDIRECT_URI'],
        scope=' '.join(app.config['OAUTH_SCOPES']),
        state=session['state'])


@app.route('/callback')
def callback():
    """Validate callback and trade in code for a token."""

    if rate_limit.check(request.remote_addr):
        app.logger.warning('Rate limiting callback.')
        return _render(error='Too many requests.'), 429
    elif session.get('state', object()) != request.args.get('state'):
        return _render(error='Bad callback, state did not match.'), 400
    elif 'error' in request.args:
        return _render(error=request.args['error']), 400

    try:
        result = oauth.fetch(app.config['OAUTH_TOKEN_URI'],
                             app.config['OAUTH_CLIENT_ID'],
                             app.config['OAUTH_CLIENT_SECRET'],
                             grant_type='authorization_code',
                             redirect_uri=app.config['OAUTH_REDIRECT_URI'],
                             code=request.args.get('code'))
    except (oauth.FetchException, ValueError) as e:
        app.logger.error('Token fetch failed: %s', e)
        return _render(error='Token fetch failed.'), 500

    del session['state']  # Delete the state in case of replay.

    if 'error' in result:
        app.logger.warning('Token fetch failed: %s', result)
        return _render(error=result['error']), 400

    client_id = db.generate_id()
    client_secret = crypto.generate_key()
    token = crypto.dumps(client_secret, result)

    with db.cursor() as cursor:
        try:
            cursor.execute(
                'INSERT INTO tokens (client_id, token) VALUES (?, ?)',
                (client_id, token))
        except db.IntegrityError:
            app.log.warning('Could not get unique client id: %s', client_id)
            return _render(error='Could not get unique client id.'), 500

    return _render(client_id=client_id, client_secret=client_secret)


@app.route('/token', methods=['POST'])
def token():
    """Validate token request, refreshing when needed."""

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

    client_limit = rate_limit.check(client_id)
    addr_limit = rate_limit.check(request.remote_addr)
    if client_limit or addr_limit:
        app.logger.warning('Rate limiting: client_id=%s address=%s',
                           client_limit, addr_limit)
        raise oauth.Error('invalid_request', 'Too many requests.')

    if not client_id or not client_secret:
        raise oauth.Error('invalid_client',
                          'Both client_id and client_secret must be set.')

    with db.cursor() as cursor:
        cursor.execute(
            'SELECT token FROM tokens WHERE client_id = ?', (client_id,))
        row = cursor.fetchone()

    if row is None:
        raise oauth.Error('invalid_client', 'Client not known.')
    elif row[0] is None:
        raise oauth.Error('invalid_grant', 'Grant has been revoked.')

    try:
        result = crypto.loads(client_secret, row[0])
    except crypto.InvalidToken:
        # Always return same message as for client not found to avoid leaking
        # valid clients directly, timing attacks could of course still work.
        raise oauth.Error('invalid_client', 'Client not known.')

    if 'refresh_token' not in result:
        return jsonify(result)

    try:
        refresh_result = oauth.fetch(app.config['OAUTH_REFRESH_URI'],
                                     app.config['OAUTH_CLIENT_ID'],
                                     app.config['OAUTH_CLIENT_SECRET'],
                                     grant_type='refresh_token',
                                     refresh_token=result['refresh_token'])
    except (oauth.FetchException, ValueError) as e:
        app.logger.error('Token refresh failed: %s', e)
        # Server error isn't currently allowed, but fixing this has been
        # brought up in https://www.rfc-editor.org/errata_search.php?eid=4745
        raise oauth.Error('server_error', 'Token refresh failed.')

    if 'error' in refresh_result:
        # Client Credentials access token responses use the same errors
        # as Authorization Code Grant access token responses. As such just
        # raise the error we got.
        raise oauth.Error(refresh_result['error'],
                          refresh_result.get('error_description'),
                          refresh_result.get('error_uri'))

    result.update(refresh_result)
    token = crypto.dumps(client_secret, result)

    with db.cursor() as cursor:
        cursor.execute('UPDATE tokens SET token = ? WHERE client_id = ?',
                       (token, client_id))

    del result['refresh_token']
    return jsonify(result)


@app.route('/revoke', methods=['POST'])
def revoke():
    """Sets the clients token to null."""

    if rate_limit.check(request.remote_addr):
        app.logger.warning('Rate limiting revoke.')
        return _render(error='Too many requests.'), 429
    elif 'client_id' not in request.form:
        return _render(error='Missing client_id.'), 400

    with db.cursor() as cursor:
        cursor.execute('UPDATE tokens SET token = null WHERE client_id = ?',
                       (request.form['client_id'],))

    # We always report success as to not leak info.
    return _render(error='Revoked client_id.'), 200


def _render(client_id=None, client_secret=None, error=None):
    return render_template_string(
        app.config['OAUTH_CALLBACK_TEMPLATE'],
        client_id=client_id, client_secret=client_secret, error=error)
