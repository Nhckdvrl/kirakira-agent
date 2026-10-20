Kirakira imports the Akashic runtime from
https://github.com/kachofugetsu09/akashic-agent at commit
`e1628ddc8a33c2840e5b4558ff3b66af207eec2a` under the MIT license.
The original copyright notice and license are in [UPSTREAM-LICENSE](UPSTREAM-LICENSE).

[UPSTREAM.json](UPSTREAM.json) records every imported production file and its
SHA-256 digest. The imported architecture and plugin implementations are upstream
work. Kirakira-specific integration, migration and evaluation code is kept outside
those pinned files. Historical implementations and evaluation results remain
available in Git history.

One local zsh snapshot fix is recorded as a patch. The Akasha message UI asset
is rebuilt from the pinned upstream frontend source and npm lock; its generated
digest is recorded separately. The upstream CLI source is relocated to
`kirakira_agent/_upstream_main.py` for the distribution entry point.
