Kirakira includes the Akashic runtime from
https://github.com/kachofugetsu09/akashic-agent at commit
`e4ba3e29ee5a259cac7eb306db91cb632ba98a13`, under the MIT license.
The original copyright notice and license are preserved in [UPSTREAM-LICENSE](UPSTREAM-LICENSE).

[UPSTREAM.json](UPSTREAM.json) records original source paths, digests, local patches
and generated assets. The base architecture and plugin implementations are upstream
work. Local presentation changes do not change that attribution.

The episodic memory plugin includes the Akasha v2 engine. Its own source provenance
is retained in [memory provenance](../plugins/akasha/UPSTREAM.json).
Kirakira's additions include session-scoped memory queries, dated and bounded
source evidence, optional candidate rank fusion, extractive evidence windows,
receipt-bound original-text reads, SDK integration and evaluation tooling. They extend the existing learning and
persistence owners rather than replacing source provenance.

Local patches also cover distribution branding, account-default compatibility,
the zsh snapshot fix, zero-turn rebuild validation, and Web build staging.
Generated plugin assets are rebuilt from registered frontend sources and the npm
lock. The original CLI is relocated to `kirakira_agent/_upstream_main.py`;
the deployment skill is relocated and adapted to the distribution command.

Unmodified scenarios in `eval/upstream/` retain their source manifest and MIT
license. Public benchmark histories are downloaded separately and are not
redistributed in the repository. Benchmark reports identify their dataset and
selection; they are this project's runs, not results reported by other projects.
