# Weekly float refresh — cron schedule

## Why cron, not a Prefect agent

The architectural decision to schedule via cron (rather than running a
Prefect agent on the droplet) is locked. Cron is simpler, has zero
runtime overhead between invocations, and the flow itself already has
all the durability primitives we need:

- Per-batch commits (PR #28)
- SIGTERM / KeyboardInterrupt handling via `_log_flow_finish_sync` (PR #29)
- Flow run logging to `flow_run_log` (PR #25)
- Per-ticker progress logging (PR #27)

The Prefect runtime is invoked when `flows.float_update_flow` is run
as a module — Prefect's `@flow` decorator handles the flow context for
ad-hoc invocations of the decorated function.

## Cron entry

Install in `/etc/cron.d/weekly-float-update` (or equivalent):

```cron
0 10 * * 0 trading cd /app/profitlyworkflow && ./venv/bin/python -m flows.float_update_flow >> /var/log/trading/float_refresh.log 2>&1
```

**Sunday 10:00 UTC = Sunday 06:00 ET during EDT** (March – November).
During EST (November – March), the entry must shift to `0 11 * * 0`.

A timezone-aware scheduling wrapper that handles the EDT/EST boundary
automatically is queued as a follow-up — for now, the operator updates
the cron entry twice a year at the DST boundary.

## Module entry point

`flows/float_update_flow.py` ends with:

```python
if __name__ == "__main__":
    import asyncio
    asyncio.run(float_update_flow())
```

Added in PR #25. No additional wiring needed for cron invocation — the
module is directly runnable as `python -m flows.float_update_flow`.

## Log output

`/var/log/trading/float_refresh.log` accumulates per-run output. The
flow itself emits per-10-ticker progress lines (PR #27) and a summary
on completion. Per-batch commits (PR #28) mean partial progress
survives a crash; the next scheduled run resumes from the unrefreshed
tail via `float_updated_at ASC NULLS FIRST`.

## flow_run_log integration

Each invocation writes a row to `flow_run_log` with `flow_name =
'weekly-float-update'`. The operator alerter (PR #36) condition 5
queries that table and clears its breached state automatically once
a successful run lands within the 8-day window.

## Verification

After installing the cron entry:

```bash
psql "$DATABASE_URL" -c "
  SELECT flow_name, status, started_at, completed_at, summary
  FROM flow_run_log
  WHERE flow_name = 'weekly-float-update'
  ORDER BY started_at DESC LIMIT 5;
"
```

Should show rows accumulating week over week with `status='completed'`
and a summary JSON containing `total / updated /
deactivated_oversized / deactivated_not_found / errors`.

## Manual invocation

The same code path supports ad-hoc backfill:

```bash
sudo -u trading bash -c '
    set -a; source /app/profitlyworkflow/.env.production; set +a
    cd /app/profitlyworkflow
    ./venv/bin/python -m flows.float_update_flow
'
```

Or via the older `scripts/update_floats.py` for more verbose progress
output.
