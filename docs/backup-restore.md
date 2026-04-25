# Backup & restore

All of Plenum's configuration — rooms, vents, schedules, thermostat settings, cycle history, event logs — lives in a single SQLite file (`app.db`) in the add-on's data directory.

## Download a backup

The **Settings** page has a **Download backup** button that serves a consistent snapshot of the database. Behind the scenes it uses SQLite's online-backup API so the file includes any unflushed WAL writes — copying `app.db` directly off the filesystem can miss recent changes.

The downloaded file is named `app.db`. Stash it somewhere safe before any risky config change.

## Restore from a backup

The **Settings** page also has a **Restore** button that accepts an `app.db` file upload. On restore:

1. The file is validated (SQLite magic bytes must match).
2. The current DB is swapped for the uploaded one.
3. The database connection is reloaded in place — the scheduler keeps running, so you don't need to restart the add-on.

## Upgrading from ≤0.6.x

Older installs stored the database as `flair.db`. On first boot after upgrade, Plenum automatically renames `flair.db` → `app.db` (along with any `-wal` / `-shm` sidecars) before opening any connection. The rename is idempotent — re-running startup after migration is a no-op.

No manual steps are needed. See [CHANGELOG](../smart_vent/CHANGELOG.md) for the full entry.
