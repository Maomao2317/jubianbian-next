# GPU priority and lossless transfer release

Baseline: `4095edc166238f7ab00f94d6b1a51807bde42f48`.
Scope is limited to GPU transport, lossless OCR pages, bounded source download,
their tests and deployment tooling. No pricing, billing, script generation,
speaker assignment, review/download rules or database schema changes.

GPU admission, upload and response have separate finite budgets. Only an
explicit pre-execution busy rejection is resubmitted. Busy admission does not
trip the failure circuit. Real failures retain CPU recovery. ASR uses the
existing model, full PCM, beam, VAD and word timestamp settings. OCR pages retain
the original sampler/crop/deduplication/merging and exact PGM pixel bytes; the
smaller of the video or lossless archive is sent. Counts, order and hashes are
validated. Empty pages remain meaningful. Upload deadlines release stuck slots.

Source downloads recover at written byte offsets with ETag/version validation.
Ignored Range, changed identity and incorrect lengths fail closed. A stalled
child is killed and reaped before its capacity slot is released. No credentials,
customer media, transcripts or signed URLs are included in the patch or records.

Review covers timeout boundaries, admission rejection safety, circuit epochs,
archive ordering/limits, object identity/offsets and idle deployment locking.
Local verification: 37 GPU/transfer tests passed; 106 related regressions ran
successfully (six ffmpeg-dependent Windows cases skipped); five GPU page and
concurrency checks passed. Python compilation and Git whitespace checks passed.
Local regression and isolated Linux image regression must pass before release.
The release helper overlays only eight application modules on each existing
image and preserves all unrelated environment, ports, volumes and frontend.

Deploy the pushed revision to an independent GPU worker and staging first.
Record GPU protocol, source hashes, readiness, memory and throughput. Raise ASR
inference concurrency only if measured throughput and memory justify it. Deploy
the same revision to production only after staging verification and while the
queue is empty, protected by the SQLite writer lock. Existing GPU worker remains
available until both environments have safely moved. Host release records carry
the exact pushed commit, hashes, GPU verification and environment verification.
