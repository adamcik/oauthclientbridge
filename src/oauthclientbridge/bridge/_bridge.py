import re
from collections.abc import Awaitable, Callable, Mapping
from http import HTTPStatus
from typing import Any, cast

import structlog
from opentelemetry import trace

from oauthclientbridge import client, crypto, db, execution, oauth, telemetry, types
from oauthclientbridge.errors import OAuthError
from oauthclientbridge.settings import LogLevel, Settings
from oauthclientbridge.utils import uri as uri_utils

from . import _basic_auth
from ._template import render_template
from ._types import BridgeResponse, Session

Fetch = Callable[..., Awaitable[dict[str, Any]]]
logger: structlog.BoundLogger = structlog.get_logger()


def _updated_fields(
    original: dict[str, Any], modified: dict[str, Any]
) -> tuple[str, ...]:
    return tuple(
        sorted(
            key
            for key in set(original).union(modified)
            if original.get(key) != modified.get(key)
        )
    )


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
            await execution.run_sync(
                "db.insert",
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

    async def token(
        self,
        *,
        form: Mapping[str, str],
        authorization: str | None,
        user_agent: str,
    ) -> BridgeResponse:
        if form.get("grant_type") != "client_credentials":
            return self._token_error(
                OAuthError.UNSUPPORTED_GRANT_TYPE,
                'Only "client_credentials" is supported.',
            )
        if "scope" in form:
            return self._token_error(
                OAuthError.INVALID_SCOPE, "Setting scope is not supported."
            )

        client_id_value = form.get("client_id")
        client_secret_value = form.get("client_secret")
        basic_credentials = _basic_auth.credentials(authorization)
        if basic_credentials is None and authorization is not None:
            return self._token_error(
                OAuthError.INVALID_CLIENT, "Only Basic Auth is supported."
            )
        if (client_id_value or client_secret_value) and basic_credentials is not None:
            return self._token_error(
                OAuthError.INVALID_REQUEST,
                "More than one mechanism for authenticating set.",
            )
        if basic_credentials is not None:
            client_id_value, client_secret_value = basic_credentials

        try:
            credentials = client.validate_credentials(
                client_id_value, client_secret_value
            )
        except client.ClientIdValidationError:
            if client_id_value is not None:
                telemetry.bind_invalid_client_id_log_context(client_id_value)
                telemetry.record_invalid_client_id_trace(client_id_value)
            return self._token_error(OAuthError.INVALID_CLIENT, "Malformed client_id.")
        except client.ClientSecretValidationError:
            return self._token_error(OAuthError.INVALID_CLIENT, "Client not known.")
        except client.CredentialValidationError as error:
            return self._token_error(OAuthError.INVALID_CLIENT, str(error))

        telemetry.set_client_id_context(credentials.client_id)
        try:
            record = await execution.run_sync(
                "db.lookup", db.lookup, credentials.client_id, self._settings.database
            )
        except LookupError:
            return self._token_error(OAuthError.INVALID_CLIENT, "Client not known.")

        if record.encrypted_token is None:
            workaround_response = self._revoked_grant_workaround_response(user_agent)
            if workaround_response is not None:
                logger.warning("Serving revoked grant workaround token")
                telemetry.record_workaround_metric("revoked_grant")
                trace.get_current_span().add_event(
                    "Served revoked grant workaround token"
                )
                return BridgeResponse(status=HTTPStatus.OK, body=workaround_response)
            return self._token_error(
                OAuthError.INVALID_GRANT, "Grant has been revoked."
            )

        try:
            result = crypto.loads(credentials.client_secret, record.encrypted_token)
        except (crypto.InvalidToken, TypeError, ValueError):
            return self._token_error(OAuthError.INVALID_CLIENT, "Client not known.")

        if "refresh_token" not in result:
            telemetry.observe_token_grant_age_metric(record.created_at)
            return BridgeResponse(status=HTTPStatus.OK, body=result)

        refresh_result = await self._fetch(
            self._settings.oauth.refresh_uri or self._settings.oauth.token_uri,
            client_id=self._settings.oauth.client_id,
            client_secret=self._settings.oauth.client_secret.get_secret_value(),
            grant_type=self._settings.oauth.grant_type,
            refresh_token=result["refresh_token"],
            endpoint="refresh",
        )
        if "error" in refresh_result:
            return await self._handle_refresh_error(credentials, refresh_result)

        if not oauth.validate_token(refresh_result):
            return self._token_error(
                OAuthError.INVALID_REQUEST, "Invalid response from provider."
            )

        if "scope" not in refresh_result and "scope" in result:
            refresh_result["scope"] = result["scope"]
        modified = oauth.scrub_refresh_token(result)
        if "refresh_token" in refresh_result:
            modified["refresh_token"] = refresh_result["refresh_token"]
            del refresh_result["refresh_token"]
        if result != modified:
            updated_fields = _updated_fields(result, modified)
            logger.warning("Updating token", updated_fields=updated_fields)
            trace.get_current_span().add_event(
                "Updating token", {"updated_fields": updated_fields}
            )
            await execution.run_sync(
                "db.update",
                db.update,
                credentials.client_id,
                crypto.dumps(credentials.client_secret, modified),
                self._settings.database,
            )

        telemetry.observe_token_grant_age_metric(record.created_at)
        return BridgeResponse(status=HTTPStatus.OK, body=refresh_result)

    async def _handle_refresh_error(
        self,
        credentials: client.ClientCredentials,
        refresh_result: dict[str, Any],
    ) -> BridgeResponse:
        refresh_outcome = oauth.token_endpoint_outcome(
            HTTPStatus.BAD_REQUEST,
            refresh_result,
            retry_status_codes=self._settings.fetch.retry_status_codes,
            error_types=self._settings.fetch.error_types,
        )
        error = refresh_outcome.normalized_error or OAuthError.SERVER_ERROR
        if refresh_outcome.invalidate_refresh_token:
            await execution.run_sync(
                "db.update",
                db.update,
                credentials.client_id,
                None,
                self._settings.database,
            )
            telemetry.record_refresh_token_invalidation_metric(error.value)
            logger.warning("Revoking stored token after upstream invalid_grant")
        elif error == OAuthError.TEMPORARILY_UNAVAILABLE:
            logger.warning(
                "Token refresh failed",
                refresh_result=oauth.sanitize_for_logging(refresh_result),
            )
        else:
            logger.error(
                "Token refresh failed",
                refresh_result=oauth.sanitize_for_logging(refresh_result),
            )
        trace.get_current_span().add_event(
            "refresh_error", oauth.sanitize_for_logging(refresh_result)
        )
        return self._token_error(
            error,
            refresh_result.get("error_description"),
            refresh_result.get("error_uri"),
            refresh_result.get("retry_after"),
        )

    def _token_error(
        self,
        error: OAuthError,
        description: str | None = None,
        uri: str | None = None,
        retry_after: Any = None,
    ) -> BridgeResponse:
        body = error.json(description=description)
        if uri is not None:
            body["error_uri"] = uri
        headers: dict[str, str] = {}
        status = HTTPStatus.BAD_REQUEST
        if error == OAuthError.INVALID_CLIENT:
            status = HTTPStatus.UNAUTHORIZED
            headers["WWW-Authenticate"] = f'Basic realm="{self._settings.auth_realm}"'
        elif error == OAuthError.TEMPORARILY_UNAVAILABLE:
            status = HTTPStatus.SERVICE_UNAVAILABLE
            if retry_after is not None:
                headers["Retry-After"] = str(retry_after)
        return BridgeResponse(
            status=status, headers=headers, body=cast(types.JsonDict, body)
        )

    def _revoked_grant_workaround_response(
        self, user_agent: str
    ) -> dict[str, Any] | None:
        user_agents = self._settings.revoked_grant_workaround_user_agents
        if not user_agents or not user_agent or not re.search(user_agents, user_agent):
            return None
        return {
            "access_token": self._settings.revoked_grant_workaround_access_token,
            "token_type": "Bearer",
            "expires_in": self._settings.revoked_grant_workaround_expires_in,
        }

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
