## 2026-11-11 - Missing Authentication on API Endpoints
**Vulnerability:** Several sensitive /api/ endpoints (e.g., /api/session/state, /api/telemetry, /api/time/current) lacked the `@_require_api_token` decorator, exposing sensitive data to unauthorized requests.
**Learning:** In Flask apps with manual decorator-based auth, it is easy to forget the auth decorator on new endpoints. The vulnerability was caused by missing decorators on endpoints that return system state or historical data.
**Prevention:** Establish a pattern where all /api/ endpoints enforce authentication by default (e.g., via a middleware or  hook) instead of relying solely on endpoint-by-endpoint decorators. If decorators are necessary, use a static code analysis tool or script to ensure all /api/ endpoints apply it.
## 2024-05-24 - Missing Authentication on API Endpoints
**Vulnerability:** Several sensitive `/api/` endpoints (e.g., `/api/session/state`, `/api/telemetry`, `/api/time/current`, `/api/history/list`) lacked the `@_require_api_token` decorator, exposing sensitive session and robot telemetry data to unauthorized requests.
**Learning:** In this Flask application, the `@_require_api_token` decorator must be explicitly placed below the `@app.route` (or blueprint route) decorator. Omitting it completely bypasses authentication.
**Prevention:** Always verify that newly created or modified `/api/` routes include the `@_require_api_token` decorator unless explicitly intended to be public.
