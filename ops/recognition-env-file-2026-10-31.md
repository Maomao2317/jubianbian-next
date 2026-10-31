# Provider environment recovery, 2026-10-31

The staging and production containers had been recreated without the private
provider environment file. Compose interpolated the provider key variables as
empty strings, so video recognition failed before contacting a provider. The
server-side `fangzhou.env` files were intact.

The Compose services now load `./fangzhou.env` into the container and no longer
override provider credentials with empty interpolation values. No credentials
or customer task data are included in this record or in Git.

- Reviewed and pushed fix: `0a114fe9fbc11486177a5ebce9f37a9222b32cd6` on
  `codex/repair-batch-content`.
- Staging and production were deployed from the same pushed revision after the
  idle-task check passed in each environment.
- Both public `/api/health` endpoints returned `status=ok`, the expected
  environment, and build `0a114fe9fbc11486177a5ebce9f37a9222b32cd6` at
  `2026-10-31T08:12:37Z`.
- Staging loaded two distinct Ark keys; production loaded three distinct Ark
  keys. Key values were not printed or recorded.
- Both application containers were healthy. Production retained the external
  data volume `jubianbian_jbb-data-seoul-20261019r2`; staging retained its
  existing data volume.
- The 39 deployed application source files matched the pushed source before
  deployment. The configuration-only change passed Compose validation and
  `git show --check`.
