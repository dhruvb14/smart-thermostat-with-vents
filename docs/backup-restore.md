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

## Finding the data directory

The database location depends on how Plenum is installed.

**HA OS / Supervised**

The on-disk path is not `/addon_configs` (that Samba share holds add-on *configuration* files). The data lives under the Supervisor's data tree. To find the exact path, run from the HAOS SSH terminal:

```bash
docker inspect $(docker ps -q --filter name=plenum) --format '{{ json .Mounts }}' | python3 -m json.tool
```

Look for the entry where `"Destination": "/data"` — the `"Source"` field is the host path, typically:

```
/mnt/data/supervisor/addons/data/<repo_id>_plenum/
```

Confirm the database is there:

```bash
ls -lh /mnt/data/supervisor/addons/data/<repo_id>_plenum/
```

**Docker**

The database is at whatever host path you bound to `/data` with `-v /path/to/data:/data`. If you did not specify a `-v` mount, the file exists only inside the ephemeral container layer and is lost on restart.

---

## Upgrading from ≤0.6.x

Older installs stored the database as `flair.db`. On first boot after upgrade, Plenum automatically renames `flair.db` → `app.db` (along with any `-wal` / `-shm` sidecars) before opening any connection. The rename is idempotent — re-running startup after migration is a no-op.

No manual steps are needed. See [CHANGELOG](../smart_vent/CHANGELOG.md) for the full entry.
