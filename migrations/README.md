# Legion migrations

Applied to the shared Railway Postgres (`divine-contentment`). Additive only.

Order matters — number prefix sets sequence. Re-running is safe (`CREATE IF NOT EXISTS`,
`INSERT ... ON CONFLICT DO NOTHING`).

```bash
psql "$PG_DSN" -f migrations/0001_legion_base.sql
```

Tables written by both containers (`claude_account_state`) use `pg_try_advisory_lock(hashtext('claude_active'))`
at the application layer to prevent split-brain. Do not add DB-level exclusion constraints
on `role='active'` — the advisory lock is the contract.
