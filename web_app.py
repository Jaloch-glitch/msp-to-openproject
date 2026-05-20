"""FastAPI web UI for MSP → OpenProject importer."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import tempfile
import threading
import uuid
from typing import Optional

import requests
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

import msp_to_openproject as importer

app = FastAPI(title="MSP → OpenProject Importer")

# ── per-job state ─────────────────────────────────────────────────────────────
_jobs: dict[str, queue.Queue] = {}
_job_results: dict[str, dict] = {}

# ── queue-based log capture ───────────────────────────────────────────────────
class _QueueHandler(logging.Handler):
    """Captures log records only from the thread that created this handler."""

    def __init__(self, q: queue.Queue) -> None:
        super().__init__()
        self.q = q
        self._tid = threading.current_thread().ident

    def emit(self, record: logging.LogRecord) -> None:
        if threading.current_thread().ident == self._tid:
            self.q.put((record.levelname, self.format(record)))


_import_logger = logging.getLogger("msp_to_openproject")

# ── shared OpenProject client ─────────────────────────────────────────────────
_client = importer.OPClient(importer._DEFAULT_URL, importer._DEFAULT_KEY)


# ── helpers ───────────────────────────────────────────────────────────────────

def _op_error_detail(exc: requests.HTTPError) -> str:
    """Extract a human-readable message from an OpenProject error response."""
    try:
        body = exc.response.json()
        msg = body.get("message") or body.get("errorIdentifier") or ""
        if body.get("_embedded", {}).get("errors"):
            sub = "; ".join(
                e.get("message", "") for e in body["_embedded"]["errors"]
            )
            if sub:
                msg = f"{msg}: {sub}" if msg else sub
        return msg or f"HTTP {exc.response.status_code}"
    except Exception:
        return f"HTTP {exc.response.status_code}: {exc.response.text[:200]}"


# ── routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    tmpl = os.path.join(os.path.dirname(__file__), "templates", "index.html")
    with open(tmpl, encoding="utf-8") as f:
        return f.read()


@app.get("/api/health")
async def health():
    """Check connection to OpenProject and return instance info."""
    try:
        r = requests.get(
            f"{importer._DEFAULT_URL}/api/v3",
            auth=("apikey", importer._DEFAULT_KEY),
            timeout=5,
        )
        r.raise_for_status()
        data = r.json()
        return {
            "ok": True,
            "url": importer._DEFAULT_URL,
            "version": data.get("coreVersion", "unknown"),
        }
    except requests.ConnectionError:
        return JSONResponse(
            status_code=503,
            content={"ok": False, "error": f"Cannot reach {importer._DEFAULT_URL}"},
        )
    except requests.HTTPError as e:
        if e.response.status_code in (401, 403):
            return JSONResponse(
                status_code=401,
                content={"ok": False, "error": "Invalid API key"},
            )
        return JSONResponse(
            status_code=502,
            content={"ok": False, "error": f"OpenProject returned {e.response.status_code}"},
        )


@app.get("/api/jvm-check")
def jvm_check():
    """Verify Java and mpxj are working. Returns a detailed diagnostic report."""
    import glob

    results = []

    def check(label, ok, detail=""):
        results.append({"label": label, "ok": ok, "detail": detail})

    # JPype available
    try:
        import jpype as _jp
        check("JPype1 installed", True, _jp.__version__)
    except ImportError:
        check("JPype1 installed", False, "pip install JPype1")
        return {"ok": False, "results": results}

    # mpxj available
    try:
        import mpxj as _mpxj
        check("mpxj installed", True, _mpxj.mpxj_dir)
    except ImportError:
        check("mpxj installed", False, "pip install mpxj")
        return {"ok": False, "results": results}

    # JVM path
    try:
        jvm_path = _jp.getDefaultJVMPath()
        check("JVM path found", True, jvm_path)
    except Exception as e:
        check("JVM path found", False, str(e))
        return {"ok": False, "results": results}

    # Start JVM (idempotent)
    if not _jp.isJVMStarted():
        try:
            for jar in glob.glob(os.path.join(_mpxj.mpxj_dir, "*.jar")):
                _jp.addClassPath(jar)
            _jp.startJVM(jvm_path, convertStrings=True)
            check("JVM started", True)
        except Exception as e:
            check("JVM started", False, str(e))
            return {"ok": False, "results": results}
    else:
        check("JVM started", True, "already running")

    # Java version
    try:
        from jpype import JClass
        System = JClass("java.lang.System")
        java_ver  = str(System.getProperty("java.version"))
        java_home = str(System.getProperty("java.home"))
        check("Java version", True, java_ver)
        check("Java home",    True, java_home)
    except Exception as e:
        check("Java version", False, str(e))

    # mpxj reader
    try:
        JClass("org.mpxj.reader.UniversalProjectReader")
        check("mpxj reader loaded", True)
    except Exception as e:
        check("mpxj reader loaded", False, str(e))

    all_ok = all(r["ok"] for r in results)
    return {"ok": all_ok, "results": results}


@app.get("/api/projects")
async def list_projects():
    """Return all accessible OpenProject projects."""
    try:
        projects = _client.list_projects()
        return [
            {"id": p["id"], "name": p["name"], "identifier": p["identifier"]}
            for p in sorted(projects, key=lambda p: p["name"].lower())
        ]
    except requests.ConnectionError:
        raise HTTPException(503, f"Cannot reach OpenProject at {importer._DEFAULT_URL}")
    except requests.HTTPError as e:
        if e.response.status_code in (401, 403):
            raise HTTPException(401, "Invalid or insufficient API key")
        raise HTTPException(502, _op_error_detail(e))


@app.get("/api/projects/{project_id}")
async def get_project(project_id: int):
    """Verify a project ID exists and return its details."""
    try:
        data = _client._get(f"/api/v3/projects/{project_id}")
        return {"id": data["id"], "name": data["name"], "identifier": data["identifier"]}
    except requests.HTTPError as e:
        if e.response.status_code == 404:
            raise HTTPException(404, f"Project ID {project_id} not found")
        if e.response.status_code in (401, 403):
            raise HTTPException(403, "No access to this project")
        raise HTTPException(502, _op_error_detail(e))


@app.get("/api/export/{project_id}")
def export_project(project_id: int):
    """Download a project as MSPDI XML (opens in MS Project)."""
    try:
        proj_info = _client._get(f"/api/v3/projects/{project_id}")
    except requests.HTTPError as e:
        if e.response.status_code == 404:
            raise HTTPException(404, f"Project ID {project_id} not found")
        raise HTTPException(502, _op_error_detail(e))

    try:
        xml_bytes = importer.export_to_mspdi(project_id, _client)
    except Exception as e:
        raise HTTPException(500, str(e))

    filename = (proj_info.get("identifier") or f"project-{project_id}") + ".xml"
    return Response(
        content=xml_bytes,
        media_type="application/xml",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/import")
async def start_import(
    file: UploadFile = File(...),
    project_id: Optional[int] = Form(None),
    create_project: Optional[str] = Form(None),
    identifier: Optional[str] = Form(None),
    overwrite: bool = Form(False),
):
    # ── Input validation ───────────────────────────────────────────────────
    if not project_id and not create_project:
        raise HTTPException(400, "Provide a project_id or a project name to create")

    filename = file.filename or "upload"
    ext = os.path.splitext(filename)[1].lower()
    if ext not in (".mpp", ".mspdi", ".mpx", ".xml"):
        raise HTTPException(
            400,
            f"Unsupported file type '{ext}'. Upload a .mpp, .mspdi, or .mpx file.",
        )

    if create_project:
        name = create_project.strip()
        if not name:
            raise HTTPException(400, "Project name cannot be empty")
        if len(name) > 255:
            raise HTTPException(400, "Project name is too long (max 255 characters)")

        slug = (identifier or "").strip() or name.lower()[:40].replace(" ", "-")
        # Validate slug format
        import re
        if not re.match(r'^[a-z0-9][a-z0-9\-_]*$', slug):
            raise HTTPException(
                400,
                "Project identifier must start with a letter/number and contain only "
                "lowercase letters, numbers, hyphens, and underscores.",
            )
    else:
        # Verify project exists before bothering to parse the file
        try:
            _client._get(f"/api/v3/projects/{project_id}")
        except requests.HTTPError as e:
            if e.response.status_code == 404:
                raise HTTPException(404, f"Project with ID {project_id} does not exist")
            if e.response.status_code in (401, 403):
                raise HTTPException(
                    403, f"You don't have access to project ID {project_id}"
                )
            raise HTTPException(502, _op_error_detail(e))

    # ── Save upload to temp file ───────────────────────────────────────────
    content = await file.read()
    if not content:
        raise HTTPException(400, "The uploaded file is empty")

    # delete=False + explicit close is required on Windows — NamedTemporaryFile
    # holds an exclusive lock while open, which blocks Java from reading the file.
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
    try:
        tmp.write(content)
    finally:
        tmp.close()

    # ── Launch import thread ───────────────────────────────────────────────
    job_id = str(uuid.uuid4())
    q: queue.Queue = queue.Queue()
    _jobs[job_id] = q

    def run() -> None:
        handler = _QueueHandler(q)  # _tid captured here = worker thread ID
        handler.setFormatter(logging.Formatter("%(message)s"))
        _import_logger.addHandler(handler)
        pid = project_id
        try:
            # Start JVM and parse the file before creating anything in OpenProject.
            # If Java is missing or the file is unreadable the job fails here,
            # leaving no orphaned empty project behind.
            _import_logger.info("Validating file and starting JVM…")
            try:
                importer._start_jvm()
                importer._load_project(tmp.name)
            except SystemExit as e:
                raise RuntimeError(str(e))
            except Exception as e:
                raise RuntimeError(f"Could not read project file: {e}")

            if overwrite and pid:
                deleted = _client.clear_project(pid)
                _import_logger.info(
                    "Cleared %d work packages. Proceeding with import…", deleted
                )

            if create_project:
                _import_logger.info(
                    "Creating project '%s' (slug: %s) …", create_project.strip(), slug
                )
                try:
                    proj = _client.create_project(create_project.strip(), slug)
                    pid = int(proj["id"])
                    _import_logger.info("Project created: ID=%d", pid)
                except requests.HTTPError as e:
                    detail = _op_error_detail(e)
                    if e.response.status_code == 422:
                        raise RuntimeError(
                            f"Could not create project: {detail}. "
                            "The identifier may already be taken — try a different one."
                        )
                    raise RuntimeError(f"Could not create project: {detail}")

            importer.import_msp(tmp.name, pid, _client)

            project_href = f"{importer._DEFAULT_URL}/projects/{pid}/work_packages"
            _job_results[job_id] = {
                "success": True,
                "project_id": pid,
                "project_url": project_href,
            }

        except RuntimeError as e:
            _import_logger.error("Error: %s", e)
            _job_results[job_id] = {"success": False, "error": str(e)}

        except requests.HTTPError as e:
            detail = _op_error_detail(e)
            _import_logger.error("OpenProject API error: %s", detail)
            _job_results[job_id] = {"success": False, "error": detail}

        except Exception as e:
            _import_logger.error("Unexpected error: %s", e)
            _job_results[job_id] = {"success": False, "error": str(e)}

        finally:
            _import_logger.removeHandler(handler)
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
            q.put(None)  # sentinel — signals SSE generator to close

    threading.Thread(target=run, daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/stream/{job_id}")
async def stream_log(job_id: str):
    """Server-Sent Events endpoint — streams log messages for a running import job."""
    q = _jobs.get(job_id)
    if q is None:
        raise HTTPException(404, "Job not found (may have already completed)")

    async def event_gen():
        last_heartbeat = asyncio.get_event_loop().time()
        try:
            while True:
                try:
                    item = q.get_nowait()
                    if item is None:
                        result = _job_results.pop(job_id, {"success": False, "error": "No result"})
                        _jobs.pop(job_id, None)
                        yield f"event: done\ndata: {json.dumps(result)}\n\n"
                        break
                    level, message = item
                    yield f"event: log\ndata: {json.dumps({'level': level, 'message': message})}\n\n"
                    last_heartbeat = asyncio.get_event_loop().time()
                except queue.Empty:
                    now = asyncio.get_event_loop().time()
                    if now - last_heartbeat > 15:
                        yield ": heartbeat\n\n"
                        last_heartbeat = now
                    await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            pass  # client disconnected — nothing to clean up here

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
