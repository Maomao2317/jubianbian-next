# Local source synchronization, 2026-11-28

This integration combines the adaptation workflow at `3ef19be` with the
production Ark transport recovery at `aa7d073`, and retains the local
adaptation project UI and availability changes from the primary checkout.

The resulting source keeps the administrator task controls, multilingual
pricing, adaptation progress and character introductions, project filtering
and confirmed deletion, and provider availability handling. GPU transport now
preserves an ambiguous connection error when HTTP response parsing has
already closed the socket, without resubmitting inference.

## Review and validation

- Reviewed the merge and local overlays, including API availability, frontend
  event handling, cache versions, transport retry boundaries and release file
  lists. `git diff --check` passed.
- Backend and operations: 66 isolated test modules, 682 tests, zero failures
  and zero errors. Three existing integration checks were skipped: the real
  Tesseract OCR check and two FFprobe-dependent encoding checks, because
  those tools were unavailable locally. FFmpeg frame extraction checks ran.
- Frontend: all nine `test-*.cjs` scripts passed, covering adaptation, project
  lists, administrator billing, uploads, progress, pricing and workspace UI.
- Windows test fixtures used short temporary paths because scoped media
  caches exceed the Windows path limit under deeply nested test directories.
  This required no change to production cache behavior.

## Deployment status

This is a source synchronization only. No staging or production deployment
was performed. A later release must deploy the pushed revision to staging,
verify it there, and then deploy the same revision to production according
to `AGENTS.md`.
