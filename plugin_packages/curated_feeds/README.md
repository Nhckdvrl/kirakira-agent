# Curated Feeds

Reference-style proactive source plugin. It declares one stdio MCP server, one
content source with exact ACK semantics, and three plugin-distributed Drift
skills. Runtime state lives in
`<workspace>/.kirakira/plugin-data/curated-feeds/`.

User configuration belongs in `config.local.toml` under that data directory.
The MCP server supports `kind = "rss"` (RSS or Atom), `kind = "webpage"`
(content-hash change monitoring), and `kind = "wordpress"` (WordPress REST
collections). `kind = "yahoo_market"` emits threshold/band-based market moves
instead of creating a new event on every quote tick. A feed may declare `urls`
for ordered fallback (used by public X timeline RSS bridges).
