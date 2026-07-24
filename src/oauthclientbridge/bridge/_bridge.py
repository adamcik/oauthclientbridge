from collections.abc import Awaitable, Callable, Mapping
from http import HTTPStatus
from typing import Any

import anyio
import structlog
from opentelemetry import trace

from oauthclientbridge import crypto, db, oauth, telemetry
from oauthclientbridge.errors import OAuthError
from oauthclientbridge.settings import LogLevel, Settings
from oauthclientbridge.utils import uri as uri_utils

from ._template import render_template
from ._types import BridgeResponse, Session

Fetch = Callable[..., Awaitable[dict[str, Any]]]
logger: structlog.BoundLogger = structlog.get_logger()


class Bridge:
    def __init__(self, settings: Settings, fetch: Fetch):
        self._settings = settings
        self._fetch = fetch

    async def authorize(self, *, query: Mapping[str, str]) -> BridgeResponse:
        redirect_uri = query.get("redirect_uri")
        if redirect_uri and redirect_uri != self._settings.oauth.redirect_uri:
            return self._authorization_error(
                OAuthError.INVALID_REQUEST, "Wrong redirect_uri."
            )

        scope = query.get("scope", " ".join(self._settings.oauth.scopes))
        if self._settings.oauth.allowed_scopes is not None and not set(
            scope.split()
        ).issubset(self._settings.oauth.allowed_scopes):
            return self._authorization_error(
                OAuthError.INVALID_SCOPE, "Requested scope is not allowed."
            )

        state = crypto.generate_key()
        session: Session = {"state": state}

        client_state = query.get("state")
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

    async def callback(
        self, *, query: Mapping[str, str], session: Session
    ) -> BridgeResponse:
        client_state = session.get("client_state")
        state = session.get("state")
        cleared_session: Session = {}

        if not query:
            return self._callback_error(
                OAuthError.INVALID_REQUEST,
                "No arguments provided, request is invalid.",
                client_state,
                cleared_session,
            )

        if state is None:
            return self._callback_error(
                OAuthError.INVALID_STATE,
                "State is not set, this page was probably refreshed.",
                client_state,
                cleared_session,
            )

        if state != query.get("state"):
            return self._callback_error(
                OAuthError.INVALID_STATE,
                "State does not match callback state.",
                client_state,
                cleared_session,
            )

        if "error" in query:
            error = oauth.normalize_error(
                query["error"],
                allowed_types=oauth.AUTHORIZATION_ERRORS,
                fallback_type=OAuthError.SERVER_ERROR,
                error_types=self._settings.fetch.error_types,
            )
            return self._callback_error(
                error, error.description, client_state, cleared_session
            )

        code = query.get("code")
        if not code:
            return self._callback_error(
                OAuthError.INVALID_REQUEST,
                "Authorization code missing from provider callback.",
                client_state,
                cleared_session,
            )

        result = await self._fetch(
            self._settings.oauth.token_uri,
            client_id=self._settings.oauth.client_id,
            client_secret=self._settings.oauth.client_secret.get_secret_value(),
            code=code,
            grant_type="authorization_code",
            redirect_uri=self._settings.oauth.redirect_uri,
            endpoint="token",
        )
        if "error" in result:
            error = oauth.normalize_error(
                result["error"],
                allowed_types=oauth.TOKEN_ERRORS,
                fallback_type=OAuthError.SERVER_ERROR,
                error_types=self._settings.fetch.error_types,
            )
            sanitized_result = oauth.sanitize_for_logging(result)
            logger.warning("Retrieving token failed", result=sanitized_result)
            trace.get_current_span().add_event("token_error", sanitized_result)
            return self._callback_error(
                error,
                error.description,
                client_state,
                cleared_session,
                retry_after=result.get("retry_after"),
            )

        if not oauth.validate_token(result):
            return self._callback_error(
                OAuthError.INVALID_RESPONSE,
                "Invalid response from provider.",
                client_state,
                cleared_session,
            )

        if "refresh_token" in result:
            result = oauth.scrub_refresh_token(result)
        client_secret = crypto.generate_key()
        client_id = db.generate_id()
        try:
            await anyio.to_thread.run_sync(  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue] # AnyIO's local stub omits worker-thread support.
                db.insert,
                client_id,
                crypto.dumps(client_secret, result),
                self._settings.database,
                tuple(sorted(result)),
                trace.get_current_span(),
            )
        except db.IntegrityError:
            return self._callback_error(
                error="integrity_error",
                description="Database integrity error.",
                state=client_state,
                session=cleared_session,
            )

        return self._callback_response(
            client_id=str(client_id),
            client_secret=client_secret,
            state=client_state,
            session=cleared_session,
        )

    def _callback_error(
        self,
        error: OAuthError | str,
        description: str,
        state: str | None,
        session: Session,
        *,
        retry_after: Any = None,
    ) -> BridgeResponse:
        status = HTTPStatus.BAD_REQUEST
        if error == OAuthError.INVALID_CLIENT:
            status = HTTPStatus.UNAUTHORIZED
        elif error == OAuthError.TEMPORARILY_UNAVAILABLE:
            status = HTTPStatus.SERVICE_UNAVAILABLE
        response = self._callback_response(
            error=error.value if isinstance(error, OAuthError) else error,
            description=description,
            state=state,
            session=session,
            status=status,
        )
        error_code = error.value if isinstance(error, OAuthError) else error
        trace.get_current_span().set_status(
            trace.Status(trace.StatusCode.ERROR, f"{error_code}: {description}")
        )
        trace.get_current_span().add_event(
            "error", {"exception.message": f"{error_code}: {description}"}
        )
        telemetry.record_server_error_metric(status, error_code, endpoint="callback")
        logger.log(
            self._settings.error_levels.get(error_code, LogLevel.ERROR),
            f"Callback failed {error_code}: {description}",
        )
        if retry_after is not None and status == HTTPStatus.SERVICE_UNAVAILABLE:
            response = self._callback_response(
                error=error.value if isinstance(error, OAuthError) else error,
                description=description,
                state=state,
                session=session,
                status=status,
                headers={"Retry-After": str(retry_after)},
            )
        return response

    def _callback_response(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        state: str | None = None,
        error: str | None = None,
        description: str | None = None,
        session: Session | None = None,
        status: HTTPStatus = HTTPStatus.OK,
        headers: dict[str, str] | None = None,
    ) -> BridgeResponse:
        response = render_template(
            self._settings.callback_template,
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "state": state,
                "error": error,
                "description": description,
            },
            status=status,
            headers=headers,
            content_security_policy=self._settings.callback_content_security_policy,
        )
        return BridgeResponse(response.status, response.headers, response.body, session)

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
            status=HTTPStatus.BAD_REQUEST,
            content_security_policy=self._settings.callback_content_security_policy,
        )
