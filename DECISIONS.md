# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.**
   - On `employees`: `{emp_code: 1}` (unique) guarantees employee uniqueness and speeds up lookups; `{department: 1, emp_code: 1}` backs filtered employee listing; `{department: 1, joined_on: 1}` backs department analytics.
   - On `attendance_logs`: `{emp_code: 1, date: 1}` (unique) prevents duplicate daily logs and optimizes monthly lookups; `{date: -1, emp_code: 1}` satisfies default attendance sorting via `IXSCAN`; `{emp_code: 1, punch_in: -1}` speeds up punch-out candidate lookups; `{date: 1, late_minutes: 1}` supports leaderboard matching.
   - *Rejected index*: A standalone `{date: 1}` index was rejected because the compound `{date: -1, emp_code: 1}` index prefix already serves date-range queries without redundant write overhead.

2. **Punch-in race.**
   Both concurrent requests validate payload and calculate the attendance date independently. Both attempt `db.attendance_logs.insert_one(doc)`. The database unique index on `(emp_code, date)` atomically admits exactly one document (HTTP 201). The loser encounters a MongoDB `DuplicateKeyError`, which our endpoint catches and immediately maps to HTTP 409.

3. **Ties.**
   The leaderboard applies MongoDB's `$rank` window function (`$setWindowFields`) for standard competition ranking (1, 2, 2, 4), followed by `{"$match": {"rank": {"$lte": limit}}}`. It filters on `rank <= limit` rather than array slice, so all employees tied at or below the cutoff rank are returned, even if total rows exceed `limit`.

4. **Headcount.**
   The `department_summary` pipeline starts from the `employees` collection (filtering `joined_on <= month_end`), performing a left outer `$lookup` into `attendance_logs` with `preserveNullAndEmptyArrays: true`. Grouping by department counts unique employee IDs, ensuring every eligible employee increments headcount regardless of log count.

5. **One thing you would change** if this had to serve 100x the data.
   Maintain incremental daily rollups (via MongoDB Change Streams or a background aggregation worker) into a separate `daily_department_metrics` collection so trend and summary queries read pre-aggregated buckets instead of scanning millions of raw punch records.
