## 2024-11-02 - Sentinel: Fix missing authentication on sensitive endpoints
**Vulnerability:** Missing authentication (`@_require_api_token`) on several critical GET endpoints (e.g., `/api/session/state`, `/api/history/list`, `/api/time/current`, `/api/telemetry`), returning sensitive data without token validation.
**Learning:** In Flask architectures, GET endpoints retrieving sensitive internal state or histories may inadvertently be left unauthenticated if security decorators are only applied to POST/mutation endpoints.
**Prevention:** Always ensure endpoints returning sensitive information, especially API endpoints exposing internal state, have appropriate authentication and authorization checks (e.g., `@_require_api_token`).
