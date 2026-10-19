# Mobile admin production release - 2026-10-19

- Source commit: `81d53abb40c108fa6662d57ecaf1023913fa1266`
- Release: `mobile-admin-20261019-r1`
- Deployment tool: `d321f8fb91ac659d83238bc226b38038ad8f00c3`
- Image: `sha256:b8be0b0315b1158a4670f9f375d775cf06209c44e3fe9216c7dd10e15c47354e`
- Previous image: `sha256:369b877eee92d9a1e3482f6ffc7b38f21d1e4405ba113e4a037ce9bd0124c1e0`
- Private rollback backup: `/opt/jbb-mobile-admin-production-20261019T113243Z`

The exact image already verified on staging was promoted without rebuilding it.
The first production preflight stopped before making changes because two customer
tasks had become active. Deployment resumed only after both tasks completed.

Verification completed:

- Staging and production container sources match the 36-file release manifest.
- The image's offline backend test suite passed before the container switch.
- Production data mounts, port bindings, and all non-build environment values
  were preserved. Staging was not restarted and retained the same container.
- Production, staging, and `www` public health endpoints report `status=ok` and
  build `mobile-admin-20261019-r1`.
- Authenticated GPU health remained ready for PaddleOCR and faster-whisper ASR.
- At a 390 x 844 mobile viewport, the production admin entry, user/credit
  controls, task management, and task detail view were accessible.
- The mobile user workbench retained existing-task viewing and the four status
  filters while upload and new-task creation remained desktop-only.

No customer task was interrupted, retried, or charged by this deployment.
