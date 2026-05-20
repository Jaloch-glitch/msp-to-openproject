# Technical Reference

This document describes the internals of the MSP to OpenProject importer in enough detail to diagnose production issues, trace data problems, and extend the codebase.

---

## Table of Contents

1. [Component Overview](#1-component-overview)
2. [Data Flow](#2-data-flow)
3. [JVM Bootstrap and mpxj](#3-jvm-bootstrap-and-mpxj)
4. [OpenProject REST Client](#4-openproject-rest-client)
5. [Import Algorithm](#5-import-algorithm)
6. [Export Algorithm](#6-export-algorithm)
7. [Overwrite / Clear Algorithm](#7-overwrite--clear-algorithm)
8. [Web Application Internals](#8-web-application-internals)
9. [SSE Log Streaming](#9-sse-log-streaming)
10. [API Endpoint Reference](#10-api-endpoint-reference)
11. [Log Patterns](#11-log-patterns)
12. [Environment Variables and Defaults](#12-environment-variables-and-defaults)
13. [Known Issues and Workarounds](#13-known-issues-and-workarounds)
14. [Error Reference](#14-error-reference)
15. [Data Mapping Reference](#15-data-mapping-reference)

---

## 1. Component Overview

```
msp_to_openproject.py
    _start_jvm()              Starts JPype JVM with mpxj JARs on the classpath
    _load_project(path)       Parses any MS Project file via UniversalProjectReader
    _date(d)                  Java LocalDateTime -> "YYYY-MM-DD" string
    _percent(p)               Coerces MS Project percentage to int 0-100
    _uid(task)                Extracts integer UniqueID from an mpxj Task object
    _str(v)                   Safely converts Java objects to Python strings
    _lag_days(lag)            Converts mpxj Duration lag to integer days
    _op_relation_type(t)      Maps MS Project relation type to OpenProject relation type
    _pick_type(task, types)   Selects the best-matching OpenProject work package type
    _pick_priority(task, pri) Maps MS Project priority integer to OpenProject priority
    OPClient                  HTTP client wrapping the OpenProject REST API v3
    import_msp(path, pid, c)  Full import: WP creation pass + relations pass
    export_to_mspdi(pid, c)   Export OP project to MSPDI XML bytes
    main()                    CLI entry point

web_app.py
    OPClient instance         Shared across all requests (thread-safe reads)
    _jobs                     dict[job_id, queue.Queue] — log queues per active job
    _job_results              dict[job_id, dict] — final result per job
    _QueueHandler             logging.Handler subclass; filters by thread ID
    /api/health               GET — connectivity check
    /api/projects             GET — list all accessible projects
    /api/projects/{id}        GET — get single project
    /api/export/{id}          GET (sync) — download project as MSPDI XML
    /api/import               POST — start import job, returns job_id
    /api/stream/{job_id}      GET — SSE log stream for a running job

templates/index.html
    Single-page application (vanilla JS, no framework)
    Three panels: setup, import (progress), complete/error
    SSE client reads /api/stream/{job_id}
    Progress driven by regex parsing of log message text
```

---

## 2. Data Flow

### Import flow

```
Browser                    web_app.py                msp_to_openproject.py     OpenProject API
  |                            |                              |                      |
  | POST /api/import           |                              |                      |
  |--------------------------->|                              |                      |
  |   (file, project_id,       |  threading.Thread(run)       |                      |
  |    overwrite)              |----------------------------->|                      |
  |<-- {job_id}                |  (if overwrite)              |  DELETE /wps...      |
  |                            |  clear_project()------------>|--------------------->|
  | GET /api/stream/{job_id}   |  (SSE: clearing progress)    |                      |
  |--------------------------->|                              |                      |
  |<-- SSE: log events         |  import_msp()                |                      |
  |                            |----------------------------->|  POST /work_packages |
  |  (Phase 1: WP creation)    |  (SSE: WP created per task)  |--------------------->|
  |                            |                              |  POST /relations      |
  |  (Phase 2: relations)      |  (SSE: relations progress)   |--------------------->|
  |<-- SSE: done event         |                              |                      |
  |   {success, project_url}   |                              |                      |
```

### Export flow

```
Browser                    web_app.py                msp_to_openproject.py     OpenProject API
  |                            |                              |                      |
  | GET /api/export/{pid}      |                              |                      |
  |--------------------------->|  export_to_mspdi()           |                      |
  |                            |----------------------------->|  GET /work_packages  |
  |                            |                              |--------------------->|
  |                            |                              |  (build mpxj tree)   |
  |                            |                              |  MSPDIWriter.write() |
  |<-- XML bytes (download)    |                              |                      |
```

---

## 3. JVM Bootstrap and mpxj

The `_start_jvm()` function is called lazily on the first file parse or export. It is idempotent — `jpype.isJVMStarted()` guards against double-start.

```python
def _start_jvm():
    if jpype.isJVMStarted():
        return jpype
    jar_dir = mpxj.mpxj_dir          # pip package ships JARs here
    for jar in glob.glob(os.path.join(jar_dir, "*.jar")):
        jpype.addClassPath(jar)
    jpype.startJVM(convertStrings=True)
```

`convertStrings=True` makes JPype automatically convert Java `String` objects to Python `str`. Without this flag, string comparisons and f-string formatting would silently fail or produce unexpected `JString` objects.

**Package name change:** mpxj 12.x moved all classes from `net.sf.mpxj.*` to `org.mpxj.*`. The code uses `org.mpxj.reader.UniversalProjectReader`. If you see a `ClassNotFoundException` mentioning `net.sf.mpxj`, the installed mpxj pip package is older than version 12.

**Java version:** JPype requires the JVM to be accessible via `JAVA_HOME` or the system PATH. On macOS, `/usr/libexec/java_home` resolves this automatically. On Windows, if `java -version` works in a terminal but JPype cannot find the JVM, set `JAVA_HOME` explicitly.

**Classpath timing:** All `jpype.addClassPath()` calls must happen before `jpype.startJVM()`. This is why the classpath setup is in `_start_jvm()` rather than at module import time.

---

## 4. OpenProject REST Client

`OPClient` wraps `requests.Session` with HTTP Basic Auth (`username="apikey"`, `password=<token>`).

All requests use a 30-second timeout. The `_get()` method defaults `pageSize=500` but callers can override it via kwargs. The `_get_all_work_packages()` method pages through results using `offset` increments of 100.

**Auth format:**
```
Authorization: Basic base64("apikey:<token>")
```

**Rate limiting:** OpenProject does not publish rate limit headers. Very large projects (1000+ tasks) may see occasional 429 or 503 responses from the API. These currently propagate as `HTTPError` and abort the import. If you need to handle rate limiting, add a retry wrapper around `_post()`.

**Work package IDs are global:** OpenProject assigns work package IDs sequentially across the entire instance, not per project. A project with 100 tasks might have WP IDs ranging from 1500 to 1650 depending on what else exists on the instance.

---

## 5. Import Algorithm

The import runs in two sequential passes.

### Pass 1 — Work package creation

1. All tasks from the MSP file are collected, excluding the root task (UID 0).
2. Tasks are sorted by tree depth (parents before children). This ensures the parent WP exists in OpenProject before the child is created, allowing the `parent` link to be set.
3. For each task, a work package body is built from the task's fields (see Data Mapping Reference below).
4. The WP is created via `POST /api/v3/work_packages`.
5. The returned WP ID is stored in `uid_to_wp: dict[int, int]` (MSP UID → OP WP ID).

### Pass 2 — Relation creation

After all WPs exist, predecessors are read for each task and mapped using `uid_to_wp`. Relations are created via `POST /api/v3/work_packages/{from_id}/relations`. This two-pass approach is required because the target WP of a relation must already exist.

**Why two passes are necessary:** MS Project allows a task to be a predecessor of a task that appears earlier in the WBS outline. If relations were created during Pass 1, the target WP might not exist yet.

### Topological sort detail

```python
def depth(t, _seen=None) -> int:
    # Walk up getParentTask() chain counting hops
    # Cycle guard via _seen set
```

The sort is stable so sibling tasks retain their original order relative to each other.

---

## 6. Export Algorithm

`export_to_mspdi(project_id, client)` reconstructs an mpxj `ProjectFile` from OpenProject work packages.

1. All WPs are fetched with pagination.
2. WPs are sorted by parent depth (parents first), mirroring the import sort.
3. For each WP, a Task is added either to the project root (`pf.addTask()`) or to its parent task object (`parent_task.addTask()`), which creates the correct WBS hierarchy.
4. Dates are set as `LocalDateTime` objects with 08:00 start time and 17:00 finish time.
5. The `MSPDIWriter` writes the project to a `ByteArrayOutputStream`. The bytes are returned as Python `bytes`.

**Limitations of export:**
- Task dependencies (relations) are not included in the export. Relations require N additional API calls (one per WP to fetch `/relations`) and are not currently implemented. The exported file has hierarchy and dates but no Gantt links.
- Resource assignments are not exported. OpenProject stores assignees as user links; mapping these back to MS Project resources is not implemented.
- Custom fields are not mapped. OpenProject custom fields have no equivalent in the MSPDI schema.

---

## 7. Overwrite / Clear Algorithm

`OPClient.clear_project(project_id)` deletes all work packages before a fresh import.

1. All WPs are fetched via `_get_all_work_packages()`.
2. WPs are sorted by parent depth in **reverse** order (deepest/leaf nodes first). This avoids attempting to delete a parent that still has children, which OpenProject rejects.
3. Each WP is deleted via `DELETE /api/v3/work_packages/{id}`.
4. Progress is logged every 50 deletions.

**Failure handling:** Individual delete failures are logged as warnings and skipped. If a parent delete fails because a child delete also failed, the import will still proceed. Orphaned WPs from failed deletes will coexist with newly imported WPs in the project.

**Irreversibility:** The clear operation cannot be undone via the application. Deleted work packages are permanently removed from OpenProject. There is no recycle bin.

---

## 8. Web Application Internals

### Job lifecycle

```
POST /api/import
  -> validates input
  -> saves upload to tempfile (OS temp dir, auto-deleted on job completion)
  -> creates queue.Queue for this job_id
  -> starts daemon thread (run())
  -> returns {job_id}

GET /api/stream/{job_id}
  -> async generator polls queue.Queue with get_nowait() + asyncio.sleep(0.05)
  -> each (level, message) tuple becomes an SSE "log" event
  -> None sentinel signals end; emits SSE "done" event with result dict
```

### Thread safety

`_jobs` and `_job_results` are plain Python dicts. Python's GIL makes dict reads and writes effectively atomic for single-item operations, but this is not formally safe for concurrent access. The current implementation is safe in practice because:
- Job IDs are UUIDs (no collision)
- Entries are added before the thread starts and removed in the SSE generator after the "done" event
- No dict is iterated while being modified

If you move to a multi-process deployment, replace these dicts with a Redis-backed store.

### _QueueHandler thread filtering

Each import job creates one `_QueueHandler` instance inside the worker thread. The handler captures `threading.current_thread().ident` at construction time and uses it to filter `emit()` calls. This prevents log records from one import thread reaching a different job's SSE stream.

```python
class _QueueHandler(logging.Handler):
    def __init__(self, q):
        self._tid = threading.current_thread().ident  # must be called IN the worker thread

    def emit(self, record):
        if threading.current_thread().ident == self._tid:
            self.q.put((record.levelname, self.format(record)))
```

The handler is added to `logging.getLogger("msp_to_openproject")` and removed in the `finally` block of `run()`. This means all logging from `msp_to_openproject.py` (including `OPClient` methods) flows through the handler during an active import.

---

## 9. SSE Log Streaming

The frontend parses log messages using regular expressions to drive the progress UI. The backend does not emit structured events — it emits raw log text and the frontend extracts meaning from it.

**SSE event types:**

| Event name | Payload | Meaning |
|------------|---------|---------|
| `log`      | `{level, message}` | A single log line from the import thread |
| `done`     | `{success, project_id, project_url}` or `{success: false, error}` | Job complete |

**Frontend state machine driven by log regexes (see `index.html`):**

| Regex | Effect |
|-------|--------|
| `Clearing (\d+) existing work packages` | Shows Phase 0 card, sets total |
| `Cleared (\d+) \/ (\d+) work packages` | Updates Phase 0 progress bar |
| `Cleared (\d+) work packages.*Proceeding` | Marks Phase 0 done, activates Phase 1 |
| `Tasks in MSP file: (\d+)` | Sets total task count for Phase 1 ring |
| `\[UID \d+\] .+ -> WP#(\d+)` | Increments WP counter, adds feed item |
| `Work packages: (\d+) created` | Completes Phase 1 ring |
| `Creating relations` | Activates Phase 2 card |
| `Relations created so far: (\d+)` | Updates Phase 2 counter |
| `Relations: (\d+) created, (\d+) failed` | Completes Phase 2 ring |

If a log message format changes in the Python code, the corresponding frontend regex must be updated or the progress display will freeze at zero without error.

---

## 10. API Endpoint Reference

### GET /api/health

Returns connectivity status with the OpenProject instance.

Response 200:
```json
{"ok": true, "url": "https://...", "version": "14.3.0"}
```

Response 503 (cannot reach OP):
```json
{"ok": false, "error": "Cannot reach https://..."}
```

Response 401 (bad API key):
```json
{"ok": false, "error": "Invalid API key"}
```

---

### GET /api/projects

Returns all accessible projects, sorted alphabetically by name.

Response 200:
```json
[{"id": 5, "name": "Site Renovation 2025", "identifier": "site-renovation-2025"}, ...]
```

---

### GET /api/projects/{project_id}

Verifies a project ID exists. Useful for pre-flight validation.

Response 404: project not found
Response 403: project exists but API key has no access

---

### GET /api/export/{project_id}

Downloads the project as an MSPDI XML file.

Response 200: `Content-Type: application/xml`, `Content-Disposition: attachment; filename="<identifier>.xml"`

Response 404: project not found
Response 500: export failed (JVM error or API error)

This endpoint is **synchronous** (not async). FastAPI runs it in a thread pool automatically. The JVM call and the OpenProject pagination are both blocking.

---

### POST /api/import

Starts a background import job.

Form fields:

| Field           | Type    | Required | Description |
|-----------------|---------|----------|-------------|
| `file`          | file    | yes      | .mpp / .mspdi / .mpx file |
| `project_id`    | int     | one of   | Existing project ID |
| `create_project`| string  | one of   | Name for new project |
| `identifier`    | string  | no       | URL slug for new project (auto-derived if omitted) |
| `overwrite`     | bool    | no       | If true, clears project before import |

Response 200:
```json
{"job_id": "550e8400-e29b-41d4-a716-446655440000"}
```

Response 400: validation error (bad file type, empty file, missing project)
Response 404: project_id not found
Response 403: no access to project_id

---

### GET /api/stream/{job_id}

Server-Sent Events stream. Media type `text/event-stream`.

The stream stays open until the job completes (sentinel `None` in the queue) or the client disconnects (`asyncio.CancelledError`).

Event format:
```
event: log
data: {"level": "INFO", "message": "  ▸ [UID   42] Build foundation         → WP#1701"}

event: done
data: {"success": true, "project_id": 5, "project_url": "https://.../projects/5/work_packages"}
```

---

## 11. Log Patterns

These are the exact log lines the backend emits. Use them to trace an import in server logs or to verify the frontend regex table above.

```
INFO  Reading  /tmp/tmpXXXXXX.mpp …
INFO  Fetching OpenProject metadata …
INFO  Types: ['task', 'milestone', 'phase', ...]
INFO  Tasks in MSP file: 1522
INFO  ▸ [UID    1] Project Setup                     → WP#1600
INFO  ◆ [UID   42] Milestone: Design Approved        → WP#1641
INFO  Work packages: 1522 created / 1522 tasks
INFO  Creating relations …
INFO  Relations created so far: 50
INFO  Relations created so far: 100
INFO  Relations: 312 created, 0 failed
INFO  ────────────────────────────────────────────────────────────
INFO  Done. 1522 work packages and 312 relations imported into project ID 5.
```

Overwrite prefix (before the above):
```
INFO  Clearing 1522 existing work packages…
INFO    Cleared 50 / 1522 work packages…
INFO    Cleared 100 / 1522 work packages…
INFO  Cleared 1522 work packages. Proceeding with import…
```

New project prefix:
```
INFO  Creating project 'Site Renovation 2025' (slug: site-renovation-2025) …
INFO  Project created: ID=7
```

Error lines:
```
ERROR  POST /api/v3/work_packages → 422  {"message":"..."}
ERROR  ✗ Failed to create WP for UID 42 (Build foundation)
WARNING  Predecessor UID 10 not imported — skipping relation
WARNING    Relation WP#1600 → WP#1650 failed
ERROR  OpenProject API error: Identifier has already been taken
ERROR  Could not create project: Identifier has already been taken. The identifier may already be taken — try a different one.
```

---

## 12. Environment Variables and Defaults

| Variable              | Used in                    | Default value |
|-----------------------|----------------------------|---------------|
| `OPENPROJECT_URL`     | `msp_to_openproject._DEFAULT_URL` | `https://openproject.burhaniengineers.com` |
| `OPENPROJECT_API_KEY` | `msp_to_openproject._DEFAULT_KEY` | (see source)  |
| `JAVA_HOME`           | JPype (auto-detected)      | (system default) |
| `_JAVA_OPTIONS`       | JVM launch flags           | (not set — JVM uses defaults) |

To increase JVM heap for large projects:
```bash
export _JAVA_OPTIONS="-Xmx2g"
```

---

## 13. Known Issues and Workarounds

### JVM cannot be started twice in the same process

JPype does not support shutting down and restarting the JVM. If you ever call `jpype.shutdownJVM()`, the next call to `_start_jvm()` will fail with `OSError: JVM cannot be restarted`. This is a JPype/JNI limitation, not a bug in the application. Do not call `shutdownJVM()`.

If you need to hot-reload the server during development, restart the entire process rather than using `--reload`.

### Relations phase appears silent for a long time

Relations are created one HTTP request at a time. For a project with 300 relations, the phase takes several minutes. The frontend does not receive any log events until the first batch of 50 completes. If the feed appears frozen during Phase 2, the import is still running. Check the server log directly to confirm.

### Log4j warning on startup

When the JVM starts, the following warning appears in the server console output:

```
main ERROR Log4j API could not find a logging provider.
```

This is a warning from the Log4j API JAR bundled with mpxj. It does not affect functionality. mpxj falls back to `java.util.logging`. The warning cannot be suppressed without providing a `log4j2.xml` configuration file on the classpath.

### OpenProject returns 422 on project creation

This means the identifier (URL slug) is already taken by another project on the instance, even if it was deleted. OpenProject does not reuse identifiers. Choose a different name or manually specify a unique `--identifier`.

### Large .mpp files cause slow Phase 1 start

mpxj parses the entire file into memory before the Python loop begins. Very large files (100MB+) may take 30-60 seconds before the first `Tasks in MSP file:` log line appears. This is normal.

### User assignment silently skipped

The importer matches resource names from the MSP file against the `name`, `login`, and `email` fields of OpenProject users. If the names do not match exactly (case-insensitive), the assignment is skipped. The API key also needs admin-level user listing permissions. Check debug output (`-v` flag or browser DevTools) for `Resource 'X' not found in OP users` messages.

### Windows: backslash in file paths

The `_load_project(path)` function passes the path directly to Java's `UniversalProjectReader.read(path)`. Java on Windows accepts both forward and backward slashes in file paths. The `tempfile.NamedTemporaryFile` on Windows returns a backslash path; this is handled correctly.

### Windows: JPype install fails

If `pip install JPype1` fails with `error: Microsoft Visual C++ 14.0 or greater is required`, install the C++ Build Tools from Microsoft. See the Prerequisites section of the README.

---

## 14. Error Reference

### HTTP errors from OpenProject

| Code | Typical cause | Resolution |
|------|---------------|------------|
| 401  | Invalid or expired API key | Regenerate the token in My Account > Access tokens |
| 403  | API key lacks permission | Ensure the account has Work package management on the project |
| 404  | Project ID does not exist | Verify the project ID in the OpenProject URL |
| 409  | Work package conflict (rare) | Usually a concurrent modification; retry the import |
| 422  | Validation error (identifier taken, required field missing, wrong type) | Read the `message` field in the response body |
| 429  | Rate limited | Add retry/backoff logic in `OPClient._post()` |
| 500  | OpenProject internal error | Check the OpenProject server logs |
| 503  | OpenProject unavailable | Wait and retry |

### Application errors

| Error message | Cause |
|---------------|-------|
| `Missing dependency: pip install JPype1` | JPype1 not installed in the active venv |
| `Missing dependency: pip install mpxj` | mpxj not installed in the active venv |
| `ClassNotFoundException: org.mpxj.reader.UniversalProjectReader` | mpxj pip package version < 12 (uses old `net.sf.mpxj` package) |
| `Could not create project: Identifier has already been taken` | Slug collision; try a different name or specify `--identifier` |
| `File not found: project.mpp` | CLI path argument is wrong |
| `Unsupported file type '.docx'` | Wrong file uploaded; only .mpp, .mspdi, .mpx, .xml accepted |
| `The uploaded file is empty` | Zero-byte file was uploaded |
| `Project with ID X does not exist` | project_id does not match any accessible project |
| `JVM cannot be restarted` | `shutdownJVM()` was called; restart the process |

---

## 15. Data Mapping Reference

This table shows what is read from the MSP file and where it goes in OpenProject.

| MS Project field       | mpxj Java method                | OpenProject field            |
|------------------------|---------------------------------|------------------------------|
| Task name              | `task.getName()`                | `subject`                    |
| Start date             | `task.getStart()`               | `startDate`                  |
| Finish date            | `task.getFinish()`              | `dueDate`                    |
| Percent complete       | `task.getPercentageComplete()`  | `percentageDone`             |
| Notes                  | `task.getNotes()`               | `description.raw` (markdown) |
| WBS code               | `task.getWBS()`                 | `description.raw` (**WBS:**) |
| Duration               | `task.getDuration()`            | `description.raw`            |
| Constraint type/date   | `task.getConstraintType()` / `task.getConstraintDate()` | `description.raw` |
| Baseline start/finish  | `task.getBaselineStart()` / `task.getBaselineFinish()` | `description.raw` |
| Cost                   | `task.getCost()`                | `description.raw`            |
| Milestone flag         | `task.getMilestone()`           | WP type = "milestone"        |
| Summary flag           | `task.getSummary()`             | WP type = "phase" / "epic" / "feature" |
| Priority               | `task.getPriority().getValue()` | `priority` href (high/normal/low) |
| Parent task (WBS)      | `task.getParentTask()`          | `parent` href                |
| Resource assignment    | `task.getResourceAssignments()` | `assignee` href (first match only) |
| Predecessors           | `task.getPredecessors()`        | relation (from predecessor WP to this WP) |
| Relation type (FS/SS/FF/SF) | `rel.getType()`            | relation type (precedes/relates) |
| Relation lag           | `rel.getLag()`                  | `lagDays`                    |

### Relation type mapping

| MS Project type | OpenProject type |
|-----------------|-----------------|
| Finish-Start (FS) | `precedes` |
| Start-Start (SS)  | `relates` |
| Finish-Finish (FF)| `relates` |
| Start-Finish (SF) | `relates` |

OpenProject only has `precedes` as a directional dependency type. SS/FF/SF are mapped to `relates` (bidirectional, no scheduling constraint) as the closest available approximation. Scheduling logic is not enforced by OpenProject regardless of relation type.

### Priority mapping

MS Project priority is an integer 0-1000 (default 500 = Normal):

| MS Project value | OpenProject priority |
|------------------|---------------------|
| >= 700           | High (or Immediate if High not found) |
| 301-699          | Normal |
| <= 300           | Low |

### Work package type selection

The importer selects the work package type in this order:
1. Milestone → type named "milestone" (if available in the project)
2. Summary task → type named "phase", then "epic", then "feature"
3. All others → type named "task"
4. If none of the above names match, the first type in the project's type list is used

Type names are matched case-insensitively against the types enabled for the target project. If a project has only "Task" and "Milestone" types, all summary tasks will be created as "Task" type.
