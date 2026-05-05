# Bug Report: Scheduler Overnight Calculation Mismatch (Timezone Related)

## Summary
The scheduler appears to be evaluating active schedules using UTC wall-clock time instead of the user's local timezone. This causes schedules to start and end at the wrong times relative to the user's expectations.

## Observations
- **Current Local Time**: 9:06 PM (21:06)
- **Schedule**: Monday-Thursday, Sunday (MTWTS) 21:00:00 - 07:00:00.
- **Observed Behavior**: The UI shows "via Schedule" is active and "ends in 5h 53m".
- **Expected Behavior**: If the schedule ends at 07:00 AM local time, the remaining time at 9:06 PM should be **9 hours and 54 minutes**.

## Evidence of UTC Mismatch
The difference between the expected (9h 54m) and observed (5h 53m) remaining time is exactly **4 hours**.

- **Eastern Daylight Time (EDT)** is UTC-4.
- If the server is evaluating the schedule in UTC:
    - Local 9:06 PM Monday = **01:06 AM Tuesday UTC**.
    - The schedule end time (07:00) is interpreted as **07:00 AM UTC**.
    - Time remaining: 07:00 - 01:06 = **5 hours and 54 minutes**.
- This perfectly matches the "ends in 5h 53m" shown in the screenshot.

## Root Cause Analysis
The Python backend uses `datetime.now(UTC).astimezone()` in `smart_vent/backend/engine/room_manager.py` to determine the local time:

```python
def _find_matching_schedule(schedules: list[Schedule], now: datetime) -> Schedule | None:
    """Return the best matching schedule block for the current moment, or None."""
    local_now = now.astimezone()  # UTC-aware -> local-aware; naive -> treated as local
    current_day = local_now.weekday()  # 0=Monday
    current_time = local_now.time().replace(second=0, microsecond=0)
    ...
```

If the `TZ` environment variable is not set correctly in the container, `astimezone()` will fall back to the system's local time, which is often UTC in Docker containers.

Although `smart_vent/run.sh` attempts to set `TZ` from the Home Assistant add-on configuration, if the user hasn't specified a timezone, it defaults to `UTC`.

## Recommended Actions
1. **Validation**: Add a check in the backend or UI to warn the user if the timezone is set to UTC, as this is rarely desired for scheduling.
2. **Explicit Timezone Handling**: Consider allowing the user to set the timezone within the app's own settings, or ensure the HA-configured timezone is always passed down and used explicitly via `pytz` or `zoneinfo`.
3. **Overnight Logic Review**: While the timezone seems to be the primary cause, verify that `_schedule_active` handles day-of-week transitions correctly when "yesterday" spans a weekend (e.g., Monday morning portion checking Sunday's schedule).

---
*Created by Jules (AI Assistant)*
