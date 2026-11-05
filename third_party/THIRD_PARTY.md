Kirakira includes the Akashic runtime from
https://github.com/kachofugetsu09/akashic-agent at commit
`e4ba3e29ee5a259cac7eb306db91cb632ba98a13`, under the MIT license.
The original copyright notice and license are preserved in [UPSTREAM-LICENSE](UPSTREAM-LICENSE).

[UPSTREAM.json](UPSTREAM.json) records original source paths, digests, local patches
and generated assets. The base architecture and plugin implementations are upstream
work. Local presentation changes do not change that attribution.

Selected subsequent conversation-reference, WebSocket lifecycle, memory lookup and
Wake read-path changes were reviewed at `8b0f0afa0167ffd58b90d9337014bfa9c9c21a1c`.
Supplemental file digests and selected commits are recorded in `UPSTREAM.json`;
the runtime baseline remains pinned separately. Original-message windows and SDK
input-part support extend the imported conversation-reference implementation.

Further selected changes reviewed at `88e69687ce140e2ff14005860c1da6a4b05bb6d3`
cover cumulative message-identity summaries, durable Markdown learning checkpoints,
worker shutdown, live tool restrictions, step-limit recovery and the optional
SearXNG provider. Local integration preserves explicit reset compatibility,
background context maintenance and safe terminal notices. Source digests,
supplemental files and complete baseline-to-distribution patches are recorded
in `UPSTREAM.json`; these selections do not replace the runtime baseline pin.

Upstream was reviewed through `5ec690b2944f682441e89630256f6b47b11efc43`. What was
adopted, what was reviewed and declined with the reason, and where this
distribution deliberately diverges are all recorded under `reviewed_updates` in
[UPSTREAM.json](UPSTREAM.json). Declining is recorded as a decision rather than
left as a silent gap, so a later reader can tell the two apart.

The persistent-goal plugin (`plugins/goal/`) is imported whole from upstream at
`101b1912885c6f0d6e6c88cb236d9994afb17ee5`, together with its tests; the state
machine, store, settlement and same-source continuation are upstream work. Per-file
digests are recorded in [UPSTREAM.json](UPSTREAM.json). `OwnerTransaction.delete`
was adopted from the same upstream so the imported store keeps its version CAS.
Local work is the integration: idle planning stands down while a goal is active,
so the two proactive paths do not contact the owner about the same work.

The episodic memory plugin includes the Akasha v2 engine. Its own source provenance
is retained in [memory provenance](../plugins/akasha/UPSTREAM.json).
Kirakira's additions include session-scoped memory queries, dated and bounded
source evidence, optional candidate rank fusion, extractive evidence windows,
receipt-bound original-text reads, SDK integration and evaluation tooling. They extend the existing learning and
persistence owners rather than replacing source provenance.

The Akasha engine follows upstream through `5ddd5ebb`: blocked graph capacity,
incremental basin structure with a resident index, a lazy-heap Residual Push with
an optional compiled kernel, a matrix-backed resident feature pool, in-place
incremental checkpoint publication with schema v3, tokenizer and vector reuse on
restore, and incremental interest prototypes. Learning dynamics and pop ordering
are unchanged. Recorded in
`third_party/patches/akasha-retrieval-representation.patch`, with the compiled
kernel and the replay migration listed among the supplemental files.

Three local changes sit on top: an empty graph stays publishable, the consumption
read and member routing stay off the event loop, and a checkpoint published
before schema v3 is refused rather than republished as v3. The last one differs
from upstream deliberately: upstream registers a one-time full replay through its
migration, and this distribution's root migration catalog does not require Akasha
migrations, so refusing is what keeps an unreplayed graph from looking migrated.

Programmatic admission accepts an optional memory scope, adopted from upstream
`6d7e25bf`. The learning axis only governs whether a session is written to
memory; whether it is recalled from comes from the recall policy of each scope
dimension the session carries, so a session with no scope satisfied that
condition vacuously and kept receiving recall. Recorded in
`third_party/patches/programmatic-memory-scope.patch`.

Local patches also cover distribution branding, account-default compatibility,
the zsh snapshot fix, zero-turn rebuild validation, and Web build staging.
Generated plugin assets are rebuilt from registered frontend sources and the npm
lock. The original CLI is relocated to `kirakira_agent/_upstream_main.py`;
the deployment skill is relocated and adapted to the distribution command.

Unmodified scenarios in `eval/upstream/` retain their source manifest and MIT
license. Public benchmark histories are downloaded separately and are not
redistributed in the repository. Benchmark reports identify their dataset and
selection; they are this project's runs, not results reported by other projects.
