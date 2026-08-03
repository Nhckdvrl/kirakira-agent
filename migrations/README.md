# Workspace migrations

`yoyo/` is the only automatically executed workspace migration catalog. Startup
acquires the workspace instance lock, applies every pending migration, and records
the result in `<workspace>/migrations.sqlite3` before runtime state is opened.

Existing migration files are immutable. Add a new migration with an explicit
`__depends__` relationship; never edit or delete a migration that has shipped.

The origin migration only records Kirakira's current workspace schema. It does not
rewrite `sessions.db`, `memory/coremem.db`, `memory/akasha.db`, or Markdown memory.
