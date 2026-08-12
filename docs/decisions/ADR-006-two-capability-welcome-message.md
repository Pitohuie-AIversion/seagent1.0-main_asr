# ADR-006: Welcome Message Presents Two Capabilities

## Status

Accepted on 2026-08-12.

## Understanding Summary

- The frontend opening message introduces the SEAgent system to its users.
- It must present exactly two capabilities: knowledge Q&A, and task creation with admission.
- Knowledge Q&A is explicitly read-only and must not alter task data.
- Task creation and admission covers information collection, constraint checks, confirmation, and publication.
- The opening message must not mention or advertise an emergency mode.
- Backend routing, ASR, dialogue state, validation, persistence, and existing runtime behavior are outside this change.

## Assumptions

- This is a presentation-only change with no new performance, scale, security, privacy, reliability, or ownership requirements.
- Chinese and English welcome messages remain aligned and use the existing I18N source of truth.

## Decision Log

- Chosen: remove emergency-mode content entirely from the opening message and present two peer capabilities.
- Rejected: nest emergency handling under task creation, because the product opening must not describe an emergency mode.
- Rejected: delete backend emergency-related code, because the requested scope is limited to the opening message.

## Final Design

The welcome message contains two sections: `Knowledge Q&A` and `Task Creation & Admission`. Each section includes a short description and one example. Unit and browser E2E tests assert both sections in Chinese and English and reject emergency-mode wording. No backend behavior changes.
