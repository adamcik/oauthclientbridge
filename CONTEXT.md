# OAuth Client Bridge

The OAuth Client Bridge translates an upstream OAuth authorization-code grant
into credentials that downstream clients can use for client-credentials token
requests.

## Language

**Bridge behavior**:
The externally observable OAuth contract of a bridge route: client-visible HTTP
and the bridge's outbound upstream OAuth interactions. It excludes framework
observability and internal implementation details.
_Avoid_: Route implementation, observability implementation

**Bridge**:
The application service that realizes bridge behavior for one route request.
It is independent of the HTTP framework that invokes it.
_Avoid_: Adapter, route

**Session**:
The browser-associated OAuth state that Bridge reads and updates during an
authorization flow.
_Avoid_: Framework session, session changes

**OAuth Outcome Observer**:
The explicitly supplied observer that receives one bounded classification for
every terminal OAuth request. It is the approved observability seam for OAuth
route outcomes, is independent of telemetry implementations, and must not
change route behavior when observation fails. An outcome is emitted only after
the response it classifies has been constructed successfully.
_Avoid_: Telemetry implementation, framework instrumentation

**Unexpected OAuth Route Failure**:
A fault that prevents a Bridge route from completing its intended OAuth
operation. It is represented to the client as OAuth `server_error` and retained
as an exception only for operational observation.
_Avoid_: Ad hoc public error code

**Endpoint**:
One of the application's stable logical operations: `authorize`, `callback`,
`token`, `metrics`, or `unknown`. Endpoints are independent of framework
endpoint names and URLs, and provide the bounded identity for request outcomes
and failures. Request lifecycle signals label framework-native requests without
a known application operation, including 404 and 405 responses, as `unknown`;
only escaped application faults use `unknown` for fallback observation.
_Avoid_: Flask endpoint, HTTP path

**Upstream Grant Type**:
The RFC-defined OAuth grant type of an outbound token exchange. It is a bounded
observability identity with the values `authorization_code` and `refresh_token`;
it is distinct from a Bridge Endpoint and from an upstream token URL.
_Avoid_: Endpoint, fetch type, token endpoint

**Browser OAuth Result Page**:
The configured HTML result representation for browser OAuth flows. It renders
both expected authorization/callback outcomes and unexpected browser-route
failures using the same safe error vocabulary and security policy.
_Avoid_: Callback-only page, framework error page

**Fallback Observer**:
The explicitly supplied observer that receives the original exception when the
runtime fallback converts an application fault into a safe response. It does
not own framework-native parsing and routing responses such as 404 and 405.
_Avoid_: Framework instrumentation, OAuth outcome observer

**Request Lifecycle Observer**:
The explicitly supplied observer that records the lifecycle of a framework
request, from its start through its completed response. It owns request context
setup, sanitized request and response signals, and request-context cleanup. A
logical OAuth outcome may add bounded context during the lifecycle, but the
lifecycle observer does not classify it or capture fallback exceptions.
It completes with the safe final response even when a Fallback Observer handled
the original exception. Its inputs are sanitized, framework-neutral request and
response values.
_Avoid_: OAuth outcome observer, fallback observer, Bridge behavior

**Application Fallback Response**:
A safe, non-cacheable response produced when the runtime fallback handles an
application fault. Browser OAuth endpoints use the Browser OAuth Result Page,
token uses OAuth JSON `server_error`, and metrics uses plain text.
_Avoid_: Framework-native error response
