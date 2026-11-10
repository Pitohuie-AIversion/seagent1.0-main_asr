## 2026-11-11 - Missing Authentication on Flask Blueprints
**Vulnerability:** Found sensitive endpoints (`/api/session/state`, `/api/history/list`, `/api/telemetry`) without `@_require_api_token` protection.
**Learning:** Flask's module structure requires careful placement of custom auth decorators. They must be placed **below** the `@bp.route` decorator to correctly hook into the endpoint routing.
**Prevention:** Regularly `grep` for new `@bp.route` definitions in `src/web/` to ensure new endpoints receive the `@_require_api_token` wrapper.
