# Secondary Ark account configuration — 2026-10-26

Production previously loaded only the primary Ark API key. Staging already
held two distinct keys. The existing second key was copied entirely on the
server into production's private environment file and pinned Compose override.
The application now loads both keys and uses its existing round-robin selector.
No credentials or customer data are included in this record or in Git.

- Reviewed and pushed deployment source:
  `7ee123ddf1ca387890bde5546bcd99c1d355c371`.
- Unchanged runtime source:
  `e90d4e1a256abd8865cbe72e212df1cd6cf7f05f`.
- Build: `ark-cost-control-20261024-r2`.
- Both sites retained the same application image:
  `sha256:62a2a5e48c44fd82d975e740b4278e41e4a9df2d4d05dc9bf427a14d0e0c1541`.
- The pushed deployment revision ran in staging first, verified at
  `2026-10-26T06:22:06.683961+00:00`, then production, verified at
  `2026-10-26T06:23:06.508806+00:00`.
- Private configuration backups and deployment receipts remain under
  `/opt/jbb-secondary-ark-7ee123ddf1ca/` on the server.

The four configuration preservation/interpolation/file replacement checks
passed on Windows and on the Linux deployment host. Review confirmed an
unchanged application source scope, exact image preservation, private backup
permissions, idle gates before recreation, and configuration rollback support.
Only the app service was recreated; data mounts, ports, GPU settings, model
routing, and concurrency settings were verified unchanged.

At each site, actual minimal text requests succeeded for both keys against
`doubao-seed-2-0-lite-260428` and `doubao-seed-2-1-turbo-260628` (four calls per
site). An isolated process verified the selector's alternating key sequence.
These probes used a fixed synthetic prompt and did not create business tasks.
All 37 runtime/static source hashes matched the pushed revision. Authenticated
GPU OCR/ASR health checks passed.

A subsequent check at `2026-10-26T06:23:38.713858+00:00` confirmed both public
HTTPS health endpoints returned 200, both containers were healthy with zero
restarts, and the second key matched across each site's private environment
file, pinned override, and running container.

Worker and shared model concurrency remain 16. Distributing requests across
keys can reduce pressure on one account when provider quotas are independent.
The application still conservatively shares its quota budget and 429 cooldown
across keys. Upload/GPU capacity and per-request model latency remain potential
bottlenecks. No batch throughput comparison or speed multiplier was measured.
