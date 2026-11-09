
## 2024-05-18 - Missing Authentication on Sensitive GET Endpoints
**Vulnerability:** Several sensitive GET endpoints (`/api/time/current`, `/api/history/list`, `/api/session/state`, `/api/telemetry`) in the Flask application were missing the `@_require_api_token` decorator, allowing unauthenticated access to system state and user data.
**Learning:** The `@_require_api_token` decorator must be explicitly added to all sensitive endpoints, as the Flask blueprint does not enforce authentication globally. The pattern is that some POST endpoints were authenticated, but many GET endpoints that still returned sensitive data were left unprotected.
**Prevention:** Always verify that the `@_require_api_token` decorator is applied to all sensitive endpoints, including GET requests that expose system state, chat history, or telemetry data. Consider implementing a global or blueprint-level authentication middleware for APIs to prevent missed route decorators.
