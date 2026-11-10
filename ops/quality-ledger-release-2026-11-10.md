# Quality, queue ownership and filtered ledger totals

Release scope: the eight backend modules and three frontend files listed in
`release_quality_ledger.py`. The existing pricing poster and download quote
formatting are inherited unchanged from `5ed007a`. No database migration,
historical balance update, GPU-service restart or capacity increase is required.

HTTP handlers now only enqueue recognition; the durable worker owns execution
and its concurrency limits. Competing failed-task retries roll back a losing
charge. Truncated HTTP/SSE reads become transient errors; when a stored response
ID exists, recovery polls that response instead of creating another generation.

Evidence summaries retain more source text and cover the full timeline when
sampling is required. Conflicting known speakers remain flagged for review;
visual/action guesses cannot silently overwrite them. OS/lip-sync conflicts are
reviewable. A concurrent healthy GPU probe is shared before considering CPU
fallback. The existing physical GPU concurrency limits remain in force.

Corrective model calls reserve discounted revenue and infrastructure costs,
target a 65% gross margin and allow at most 3% above the baseline repair budget.
These are conservative admission estimates, not an exact invoice or a guaranteed
realized margin. Existing drafts remain available for owner review after repair
failure/exhaustion. Empty outputs still fail. Pending reviews block corresponding
batch/all exports; confirmed individual episodes remain downloadable.

Admin totals cover all filtered rows before pagination. Included recharge gifts
are separated only when the note reconciles to credited points. Ambiguous notes
are flagged in Other. Cash principal is distinguished from credit face value;
refunds represent returned credits. Statistics never modify balances or history.

Review: inspected queue ownership, transaction rollback, source preservation,
speaker conflict handling, bounded circuit waits, export authorization and gift
allocation. Backend regression, frontend checks and diff whitespace checks must
pass before commit/push. The release helper verifies the pushed head and runtime
baseline, tests a network-isolated image, overlays only the listed application
files and permits only image/build/budget configuration changes.

Local verification: 308 backend regressions passed, followed by 177 relevant
regressions after adding the two competing-retry rollback cases. Frontend syntax,
admin ledger, owner review, batch upload and compression-progress checks passed.
Python compilation and Git whitespace checks passed. Windows tests that create
temporary databases require the existing test permission outside the sandbox.

Release order: push verified source, prepare/deploy staging, verify UI/API,
prepare/deploy the same revision to production. A SQLite writer lock protects
the idle check through stopping the old worker. Active queues defer deployment.
Per-environment prepared/deployment JSON files on the host record the exact
commit, hashes, health, preserved configuration/mounts and task counts. Record
the pushed commit and completed verification in the final handoff.
