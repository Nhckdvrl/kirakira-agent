Kirakira imports the Akashic runtime from
https://github.com/kachofugetsu09/akashic-agent at commit
`d8761bb9000a7be8701c65e049b0978879cc6889` under the MIT license.
The original copyright notice and license are in [UPSTREAM-LICENSE](UPSTREAM-LICENSE).

[UPSTREAM.json](UPSTREAM.json) records every imported production file and its
SHA-256 digest. The imported architecture and plugin implementations are upstream
work. Kirakira-specific integration, migration and evaluation code is kept outside
those pinned files. Historical implementations and evaluation results remain
available in Git history.

A local zsh snapshot fix, the native zero-turn rebuild validation fix, and a
distribution build staging hook are recorded as patches. The Akasha message UI asset
is rebuilt from the pinned upstream frontend source and npm lock; its generated
digest is recorded separately. The upstream CLI source is relocated to
`kirakira_agent/_upstream_main.py` for the distribution entry point.

The unmodified evaluation scenarios in `eval/upstream/` carry their own source
manifest and use the same upstream license. They run against this distribution
when passed its path with `--source`.
