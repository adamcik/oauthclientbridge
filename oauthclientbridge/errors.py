ACCESS_DENIED = 'access_denied'
INVALID_CLIENT = 'invalid_client'
INVALID_GRANT = 'invalid_grant'
INVALID_REQUEST = 'invalid_request'
INVALID_SCOPE = 'invalid_scope'
INVALID_STATE = 'invalid_scope'
INVALID_RESPONSE = 'invalid_response'
SERVER_ERROR = 'server_error'
TEMPORARILY_UNAVAILABLE = 'temporarily_unavailable'
UNAUTHORIZED_CLIENT = 'unauthorized_client'
UNSUPPORTED_GRANT_TYPE = 'unsupported_grant_type'
UNSUPPORTED_RESPONSE_TYPE = 'unsupported_response_type'

ERROR_DESCRIPTIONS = {
    INVALID_REQUEST: (
        'The request is missing a required parameter, includes an invalid '
        'parameter value, includes a parameter more than once, or is '
        'otherwise malformed.'
    ),
    INVALID_CLIENT: (
        'Client authentication failed (e.g., unknown client, no client '
        'authentication included, or unsupported authentication method).'
    ),
    INVALID_GRANT: (
        'The provided authorization grant or refresh token is invalid, '
        'expired or revoked.'
    ),
    UNAUTHORIZED_CLIENT: (
        'The client is not authorized to perform this action.'
    ),
    ACCESS_DENIED: (
        'The resource owner or authorization server denied the request.'
    ),
    UNSUPPORTED_RESPONSE_TYPE: (
        'The authorization server does not support obtaining an authorization '
        'code using this method.'
    ),
    UNSUPPORTED_GRANT_TYPE: (
        'The authorization grant type is not supported by the authorization '
        'server.'
    ),
    INVALID_SCOPE: ('The requested scope is invalid, unknown, or malformed.'),
    SERVER_ERROR: (
        'The server encountered an unexpected condition that prevented it '
        'from fulfilling the request.'
    ),
    TEMPORARILY_UNAVAILABLE: (
        'The server is currently unable to handle the request due to a '
        'temporary overloading or maintenance of the server.'
    ),
}
