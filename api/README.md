# Local FastAPI API

Run from the repository root after installing the API extras:

```powershell
python -m pip install fastapi uvicorn httpx
python -m api.main
```

The server binds to `127.0.0.1` by default. `CLASSROOM_API_PORT` controls the
port, and `CLASSROOM_API_HOST` can be set by an explicit local startup
configuration when a different bind address is intentionally required.
The API uses one database for its entire lifetime, resolved by `app_storage.py`.
`GET /api/library/status` reports its identity and job state. There are no
workspace creation or switching endpoints. `CLASSROOM_DATA_ROOT` isolates a
development/test installation; ordinary launches use the saved library data directory.

The ASGI entry point is `api.main:app`, and the direct module entry point is
`python -m api.main`. The default port is `8765`.

Batch processing is available at `POST /api/batch/process` with a JSON body
containing `folders: string[]` (or existing `folder_ids: number[]`) plus the
same analysis options as the single-video endpoint. Each selected physical
folder is registered as a logical library folder, and its videos are processed
sequentially in one durable job. The job can be inspected through
`GET /api/jobs/{job_id}` or its SSE endpoint.

Video responses include `file_change_block_reason` (`null` when unlocked).
`PATCH /api/videos/{video_id}/filename` checks only that video's membership in
queued, running or resource-paused jobs, including every member of durable or
legacy batch queues. Unrelated files can be renamed while processing continues.
The backend rechecks under the submission lock; a disabled frontend control is
not the safety boundary. Completed, failed, stopped and `awaiting_api` jobs no
longer reserve files. Storage relocation still locks all files, and import/new
processing requests retain their existing global exclusion.

Cross-video subtitle search is available at `GET /api/library/search?q=...`.
It returns matching subtitle text, video metadata, millisecond jump positions,
highlight parts, and paginated counts from the single video library. See
[the search API contract](../docs/subtitle_search_api.md) for matching rules,
frontend navigation integration, and index migration details.
# Storage relocation

`POST /api/settings/directories/preview` accepts the directory settings and
returns a read-only inventory, disk capacity and `plan_id`. Submit those settings
and the plan ID to `POST /api/settings/directories/migrate` to start a background
`storage_migration` job. `GET /api/jobs/{id}` reports copy/verification progress;
`GET /api/settings/directories` includes the latest `migration_job`.

Use `media_storage_root` to relocate videos and audio together. The server derives
and creates its `videos` and `audio` subdirectories. Bound children cannot be
overridden independently. Old separate paths remain in use until an explicit
migration; GET also returns `media_storage_root`, `media_storage_bound`, and
`media_storage_default_root` for the single-directory picker.

The compatible directory `PUT` now performs the same verified relocation
synchronously. Database files, external video sources, installed Python
environments and historical logs are excluded. See
[`docs/storage-migration.md`](../docs/storage-migration.md) for boundaries and recovery.

Small application data has a separate offline flow. Preview with
`POST /api/settings/application-data/preview` (`destination`, `library_id`),
then explicitly confirm via `POST /api/settings/application-data/migrate`
with the returned `plan_id`. The latter returns `202 {id, status: "restarting"}`
and immediately invokes the verified launcher to stop, copy/verify and restart.
This is not a deferred migration. The UI must warn about the immediate restart
before submitting. Database and encrypted configuration copies go under
`library/` and `config/`; originals remain as backups. The library ID and
navigation scope remain stable. Read `application_data.migration` from
`GET /api/settings/directories` after reconnecting to inspect the result.
