from collections.abc import Awaitable, Callable
from http import HTTPStatus
from typing import Any

from oauthclientbridge import crypto
from oauthclientbridge.errors import OAuthError
from oauthclientbridge.settings import Settings
from oauthclientbridge.utils import uri as uri_utils

from ._template import render_template
from ._types import AuthorizationRequest, BridgeResponse, Session

Fetch = Callable[..., Awaitable[dict[str, Any]]]


class Bridge:
    def __init__(self, settings: Settings, fetch: Fetch):
        self._settings = settings
        self._fetch = fetch

    async def authorize(self, request: AuthorizationRequest) -> BridgeResponse:
        redirect_uri = request.query.get("redirect_uri")
        if redirect_uri and redirect_uri != self._settings.oauth.redirect_uri:
            return self._authorization_error(
                OAuthError.INVALID_REQUEST, "Wrong redirect_uri."
            )

        scope = request.query.get("scope", " ".join(self._settings.oauth.scopes))
        if self._settings.oauth.allowed_scopes is not None and not set(
            scope.split()
        ).issubset(self._settings.oauth.allowed_scopes):
            return self._authorization_error(
                OAuthError.INVALID_SCOPE, "Requested scope is not allowed."
            )

        state = crypto.generate_key()
        session: Session = {"state": state}

        client_state = request.query.get("state")
        if client_state is not None:
            session["client_state"] = client_state

        return BridgeResponse(
            status=HTTPStatus.FOUND,
            headers={
                "Location": uri_utils.rewrite_uri(
                    self._settings.oauth.authorization_uri,
                    {
                        "client_id": self._settings.oauth.client_id,
                        "response_type": "code",
                        "redirect_uri": self._settings.oauth.redirect_uri,
                        "scope": scope,
                        "state": state,
                    },
                )
            },
            session=session,
        )

    def _authorization_error(
        self, error: OAuthError, description: str
    ) -> BridgeResponse:
        variables = {
            "client_id": None,
            "client_secret": None,
            "state": None,
            "error": error.value,
            "description": description,
        }
        return render_template(
            self._settings.callback_template,
            variables,
            self._settings.callback_content_security_policy,
            status=HTTPStatus.BAD_REQUEST,
        )
