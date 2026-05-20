#!/usr/bin/env python3
"""
Import Microsoft Project (.mpp / .mspdi / .mpx) files into OpenProject.

Usage:
    python msp_to_openproject.py <file.mpp> --project-id <id>
    python msp_to_openproject.py <file.mpp> --create-project "My Project"

Env vars (defaults already set; override with CLI flags):
    OPENPROJECT_URL
    OPENPROJECT_API_KEY

Requirements: Java 8+ on PATH, pip install mpxj JPype1 requests
"""

import json
import os
import sys
import argparse
import logging
import glob
from typing import Optional

import requests

# ── Default connection details ───────────────────────────────────────────────
_DEFAULT_URL = os.environ.get(
    "OPENPROJECT_URL", "https://openproject.burhaniengineers.com"
)
_DEFAULT_KEY = os.environ.get(
    "OPENPROJECT_API_KEY",
    "opapi-f2a8fcdcd38dc4b985f887e59c55939662d92066a5ed18d72a6b301913205719",
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger(__name__)


# ── JVM / mpxj bootstrap ─────────────────────────────────────────────────────

def _start_jvm():
    """Start JPype JVM with mpxj JARs.  Must run before any Java class access."""
    try:
        import jpype
        import jpype.imports
    except ImportError:
        sys.exit("Missing dependency: pip install JPype1")

    if jpype.isJVMStarted():
        return jpype

    try:
        import mpxj as _mpxj_mod
        jar_dir = _mpxj_mod.mpxj_dir
    except ImportError:
        sys.exit("Missing dependency: pip install mpxj")

    for jar in glob.glob(os.path.join(jar_dir, "*.jar")):
        jpype.addClassPath(jar)

    # Resolve the JVM path explicitly — more reliable than letting JPype guess,
    # especially on Windows where JAVA_HOME may not be set but the registry has it.
    try:
        jvm_path = jpype.getDefaultJVMPath()
    except Exception:
        _jvm_not_found()

    try:
        jpype.startJVM(jvm_path, convertStrings=True)
    except Exception as exc:
        if "jvm.dll" in str(exc).lower() or "shared library" in str(exc).lower():
            _jvm_not_found()
        raise

    return jpype


def _jvm_not_found() -> None:
    if sys.platform == "win32":
        sys.exit(
            "No JVM found. Java is either not installed or JAVA_HOME is not set.\n\n"
            "Fix:\n"
            "  1. Install Java 11+ from https://adoptium.net (Eclipse Temurin recommended)\n"
            "  2. Open System Properties > Environment Variables\n"
            "  3. Add a System variable:  JAVA_HOME = C:\\Program Files\\Eclipse Adoptium\\jdk-21...\\  "
            "(adjust to your install path)\n"
            "  4. Edit the System PATH variable and append:  %JAVA_HOME%\\bin\n"
            "  5. Close and reopen your terminal, then restart the application.\n\n"
            "To verify: run  java -version  in a new Command Prompt."
        )
    sys.exit(
        "No JVM found. Install Java 11+ and ensure it is on your PATH.\n"
        "Verify with: java -version"
    )


def _load_project(path: str):
    """Parse any supported MSP file format and return an mpxj ProjectFile."""
    jpype = _start_jvm()
    from jpype import JClass

    # Normalise to forward slashes — required on Windows where tempfile returns
    # backslash paths that can confuse the Java file reader.
    path = path.replace("\\", "/")

    UPR = JClass("org.mpxj.reader.UniversalProjectReader")
    return UPR().read(path)


# ── Data conversion helpers ───────────────────────────────────────────────────

def _date(d) -> Optional[str]:
    """Convert Java LocalDateTime → 'YYYY-MM-DD', return None if blank."""
    if d is None:
        return None
    try:
        return f"{int(d.getYear())}-{int(d.getMonthValue()):02d}-{int(d.getDayOfMonth()):02d}"
    except Exception:
        return None


def _percent(p) -> int:
    if p is None:
        return 0
    try:
        return max(0, min(100, int(float(str(p).replace("%", "").strip()))))
    except (ValueError, TypeError):
        return 0


def _uid(task) -> Optional[int]:
    if task is None:
        return None
    try:
        return int(str(task.getUniqueID()))
    except Exception:
        return None


def _str(v) -> str:
    if v is None:
        return ""
    s = str(v)
    return "" if s.lower() in ("none", "null") else s


def _lag_days(lag) -> int:
    """Convert mpxj Duration lag → integer days."""
    if lag is None:
        return 0
    s = _str(lag)
    try:
        if s.endswith("d"):
            return round(float(s[:-1]))
        if s.endswith("w"):
            return round(float(s[:-1]) * 5)
        if s.endswith("h"):
            return round(float(s[:-1]) / 8)
        if s.endswith("m"):
            return round(float(s[:-1]) / 480)
    except Exception:
        pass
    return 0


_FS_RELATION_MAP = {
    "FS": "precedes",
    "SS": "relates",
    "FF": "relates",
    "SF": "relates",
    "FINISH_START": "precedes",
    "START_START": "relates",
    "FINISH_FINISH": "relates",
    "START_FINISH": "relates",
}


def _op_relation_type(msp_type) -> str:
    key = _str(msp_type).split(".")[-1].upper()
    return _FS_RELATION_MAP.get(key, "precedes")


def _pick_type(task, types: dict[str, str]) -> Optional[str]:
    try:
        is_milestone = bool(task.getMilestone())
    except Exception:
        is_milestone = False
    try:
        is_summary = bool(task.getSummary())
    except Exception:
        is_summary = False

    if is_milestone:
        if "milestone" in types:
            return types["milestone"]
    if is_summary:
        for k in ("phase", "feature", "epic"):
            if k in types:
                return types[k]
    if "task" in types:
        return types["task"]
    return next(iter(types.values()), None)


def _pick_priority(task, priorities: dict[str, str]) -> Optional[str]:
    try:
        val = int(str(task.getPriority().getValue()))
    except Exception:
        return priorities.get("normal")
    if val >= 700:
        return priorities.get("high") or priorities.get("immediate") or priorities.get("normal")
    if val <= 300:
        return priorities.get("low") or priorities.get("normal")
    return priorities.get("normal")


# ── OpenProject REST client ───────────────────────────────────────────────────

class OPClient:
    def __init__(self, base_url: str, api_key: str):
        self.base = base_url.rstrip("/")
        self.s = requests.Session()
        self.s.auth = ("apikey", api_key)
        self.s.headers["Content-Type"] = "application/json"

    def _get(self, path: str, **params) -> dict:
        r = self.s.get(f"{self.base}{path}", params={"pageSize": 500, **params}, timeout=30)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, body: dict) -> dict:
        r = self.s.post(f"{self.base}{path}", json=body, timeout=30)
        if not r.ok:
            log.error("POST %s → %d  %s", path, r.status_code, r.text[:400])
        r.raise_for_status()
        return r.json()

    def list_projects(self) -> list[dict]:
        return self._get("/api/v3/projects").get("_embedded", {}).get("elements", [])

    def create_project(self, name: str, identifier: str) -> dict:
        return self._post("/api/v3/projects", {"name": name, "identifier": identifier})

    def get_types(self, project_id: int) -> dict[str, str]:
        elems = (
            self._get(f"/api/v3/projects/{project_id}/types")
            .get("_embedded", {}).get("elements", [])
        )
        return {e["name"].lower(): e["_links"]["self"]["href"] for e in elems}

    def get_statuses(self) -> dict[str, str]:
        elems = (
            self._get("/api/v3/statuses")
            .get("_embedded", {}).get("elements", [])
        )
        return {e["name"].lower(): e["_links"]["self"]["href"] for e in elems}

    def get_priorities(self) -> dict[str, str]:
        elems = (
            self._get("/api/v3/priorities")
            .get("_embedded", {}).get("elements", [])
        )
        return {e["name"].lower(): e["_links"]["self"]["href"] for e in elems}

    def get_users(self) -> dict[str, str]:
        """Return {any_name_variant_lower: href}. Returns empty dict if not authorised."""
        try:
            elems = (
                self._get("/api/v3/users")
                .get("_embedded", {}).get("elements", [])
            )
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code in (403, 401):
                log.warning("Cannot list users (insufficient permissions) — assignees will be skipped.")
                return {}
            raise
        out: dict[str, str] = {}
        for u in elems:
            href = u["_links"]["self"]["href"]
            for key in ("name", "login", "email"):
                v = (u.get(key) or "").lower().strip()
                if v:
                    out[v] = href
        return out

    def create_work_package(self, project_id: int, body: dict) -> dict:
        body.setdefault("_links", {})["project"] = {
            "href": f"/api/v3/projects/{project_id}"
        }
        return self._post("/api/v3/work_packages", body)

    def create_relation(
        self, from_id: int, to_id: int, rel_type: str, lag_days: int = 0
    ) -> dict:
        body: dict = {
            "type": rel_type,
            "_links": {"to": {"href": f"/api/v3/work_packages/{to_id}"}},
        }
        if lag_days:
            body["lagDays"] = lag_days
        return self._post(f"/api/v3/work_packages/{from_id}/relations", body)

    def _get_all_work_packages(self, project_id: int) -> list[dict]:
        """Fetch all work packages in a project, handling pagination."""
        all_wps: list[dict] = []
        offset = 1
        while True:
            data = self._get(
                f"/api/v3/projects/{project_id}/work_packages",
                pageSize=100,
                offset=offset,
            )
            elements = data.get("_embedded", {}).get("elements", [])
            all_wps.extend(elements)
            if len(all_wps) >= data.get("total", 0):
                break
            offset += 100
        return all_wps

    def get_project_relations(self, wp_ids: list[int]) -> list[dict]:
        """Fetch all relations involving the given work package IDs.

        Uses the /api/v3/relations?filters= endpoint in batches of 50 IDs to
        avoid hitting URL length limits.  Returns a deduplicated list.
        """
        if not wp_ids:
            return []
        all_rels: list[dict] = []
        seen: set[int] = set()
        for i in range(0, len(wp_ids), 50):
            chunk = [str(x) for x in wp_ids[i:i + 50]]
            filters = json.dumps(
                [{"involved": {"operator": "=", "values": chunk}}]
            )
            try:
                data = self._get("/api/v3/relations", filters=filters, pageSize=500)
                for rel in data.get("_embedded", {}).get("elements", []):
                    rid = rel.get("id")
                    if rid not in seen:
                        seen.add(rid)
                        all_rels.append(rel)
            except Exception as e:
                log.warning("Could not fetch relations (batch %d): %s", i // 50, e)
        return all_rels

    def delete_work_package(self, wp_id: int) -> None:
        r = self.s.delete(
            f"{self.base}/api/v3/work_packages/{wp_id}",
            timeout=60,
        )
        r.raise_for_status()

    def clear_project(self, project_id: int) -> int:
        """Delete all work packages in a project. Returns count deleted."""
        wps = self._get_all_work_packages(project_id)
        if not wps:
            log.info("  Project is empty — nothing to clear.")
            return 0

        log.info("Clearing %d existing work packages…", len(wps))
        wp_map = {wp["id"]: wp for wp in wps}

        def _depth(wp_id: int, seen: Optional[set] = None) -> int:
            if seen is None:
                seen = set()
            if wp_id in seen:
                return 0
            seen.add(wp_id)
            wp = wp_map.get(wp_id)
            if not wp:
                return 0
            href = ((wp.get("_links") or {}).get("parent") or {}).get("href") or ""
            if not href or "null" in href:
                return 0
            try:
                return 1 + _depth(int(href.rstrip("/").split("/")[-1]), seen)
            except Exception:
                return 0

        # Delete leaves first so parents aren't blocked
        sorted_wps = sorted(wps, key=lambda w: _depth(w["id"]), reverse=True)
        deleted = 0
        for wp in sorted_wps:
            retries = 2
            while retries >= 0:
                try:
                    self.delete_work_package(wp["id"])
                    deleted += 1
                    break
                except requests.exceptions.Timeout:
                    if retries:
                        log.warning("  DELETE WP#%d timed out — retrying…", wp["id"])
                        retries -= 1
                    else:
                        log.warning("  DELETE WP#%d timed out after retries — skipping", wp["id"])
                        break
                except requests.HTTPError as e:
                    log.warning("  Could not delete WP#%d: %s", wp["id"], e)
                    break
            if deleted % 10 == 0 and deleted:
                log.info("  Cleared %d / %d work packages…", deleted, len(wps))
        return deleted


# ── Core import logic ─────────────────────────────────────────────────────────

def import_msp(mpp_path: str, project_id: int, client: OPClient) -> None:
    log.info("Reading  %s …", mpp_path)
    proj = _load_project(mpp_path)

    log.info("Fetching OpenProject metadata …")
    types      = client.get_types(project_id)
    priorities = client.get_priorities()
    users      = client.get_users()
    log.info("Types: %s", list(types.keys()))

    # ── Collect tasks, skip null root (UID 0) ──────────────────────────────
    all_tasks = []
    for t in proj.getTasks():
        if t is None:
            continue
        uid = _uid(t)
        if not uid:
            continue
        all_tasks.append(t)

    # Topological sort: parents before children
    def depth(t, _seen=None) -> int:
        if _seen is None:
            _seen = set()
        u = _uid(t)
        if u in _seen:
            return 0
        _seen.add(u)
        try:
            parent = t.getParentTask()
            pu = _uid(parent)
            if pu and pu != 0:
                return 1 + depth(parent, _seen)
        except Exception:
            pass
        return 0

    all_tasks.sort(key=depth)
    log.info("Tasks in MSP file: %d", len(all_tasks))

    # ── Create work packages ────────────────────────────────────────────────
    uid_to_wp: dict[int, int] = {}  # MSP UID → OP work-package ID

    for task in all_tasks:
        uid = _uid(task)
        name = _str(task.getName()) or f"Task {uid}"

        body: dict = {"subject": name, "_links": {}}

        # Type
        t_href = _pick_type(task, types)
        if t_href:
            body["_links"]["type"] = {"href": t_href}

        # Dates
        start  = _date(task.getStart())
        finish = _date(task.getFinish())
        if start:
            body["startDate"] = start
        if finish:
            body["dueDate"] = finish

        # Progress
        body["percentageDone"] = _percent(task.getPercentageComplete())

        # Description — aggregate useful metadata as markdown
        parts: list[str] = []
        notes = _str(task.getNotes())
        if notes:
            parts.append(notes)
        wbs = _str(task.getWBS())
        if wbs:
            parts.append(f"**WBS:** {wbs}")
        duration = _str(task.getDuration())
        if duration:
            parts.append(f"**Duration:** {duration}")
        try:
            ct = _str(task.getConstraintType())
            cd = _date(task.getConstraintDate())
            if ct and ct.lower() not in ("as_soon_as_possible", ""):
                parts.append(f"**Constraint:** {ct}" + (f" ({cd})" if cd else ""))
        except Exception:
            pass
        try:
            bs = _date(task.getBaselineStart())
            bf = _date(task.getBaselineFinish())
            if bs or bf:
                parts.append(f"**Baseline:** {bs or '?'} → {bf or '?'}")
        except Exception:
            pass
        try:
            cost = _str(task.getCost())
            if cost and cost not in ("0", "0.0", ""):
                parts.append(f"**Cost:** {cost}")
        except Exception:
            pass

        if parts:
            body["description"] = {"format": "markdown", "raw": "\n\n".join(parts)}

        # Priority
        pri = _pick_priority(task, priorities)
        if pri:
            body["_links"]["priority"] = {"href": pri}

        # Parent (WBS hierarchy → OP parent WP)
        try:
            parent = task.getParentTask()
            pu = _uid(parent)
            if pu and pu != 0:
                parent_wp = uid_to_wp.get(pu)
                if parent_wp:
                    body["_links"]["parent"] = {
                        "href": f"/api/v3/work_packages/{parent_wp}"
                    }
        except Exception:
            pass

        # Assignee (first resource with a matching OP user)
        try:
            for asgn in list(task.getResourceAssignments() or []):
                res = asgn.getResource()
                if res and res.getName():
                    rname = _str(res.getName()).lower().strip()
                    user_href = users.get(rname)
                    if user_href:
                        body["_links"]["assignee"] = {"href": user_href}
                        break
                    log.debug("Resource '%s' not found in OP users", rname)
        except Exception:
            pass

        try:
            wp  = client.create_work_package(project_id, body)
            wp_id = int(wp["id"])
            uid_to_wp[uid] = wp_id
            sym = "◆" if task.getMilestone() else "▸"
            log.info("  %s [UID %4d] %-50s → WP#%d", sym, uid, name[:50], wp_id)
        except requests.HTTPError:
            log.error("  ✗ Failed to create WP for UID %d (%s)", uid, name)

    log.info("Work packages: %d created / %d tasks", len(uid_to_wp), len(all_tasks))

    # ── Create relations (task dependencies) ───────────────────────────────
    log.info("Creating relations …")
    rel_ok = rel_fail = 0

    for task in all_tasks:
        uid = _uid(task)
        to_wp = uid_to_wp.get(uid)
        if not to_wp:
            continue

        try:
            predecessors = list(task.getPredecessors() or [])
        except Exception:
            continue

        for rel in predecessors:
            try:
                pred_uid = _uid(rel.getPredecessorTask())
            except Exception:
                continue
            if not pred_uid:
                continue

            from_wp = uid_to_wp.get(pred_uid)
            if not from_wp:
                log.warning("  Predecessor UID %d not imported — skipping relation", pred_uid)
                continue

            rtype = _op_relation_type(rel.getType())
            lag   = _lag_days(rel.getLag())

            try:
                client.create_relation(from_wp, to_wp, rtype, lag)
                rel_ok += 1
                if rel_ok % 50 == 0:
                    log.info("  Relations created so far: %d", rel_ok)
                log.debug("  WP#%d -[%s]→ WP#%d (lag %dd)", from_wp, rtype, to_wp, lag)
            except requests.HTTPError:
                rel_fail += 1
                log.warning("  Relation WP#%d → WP#%d failed", from_wp, to_wp)

    log.info("Relations: %d created, %d failed", rel_ok, rel_fail)
    log.info("─" * 60)
    log.info(
        "Done. %d work packages and %d relations imported into project ID %d.",
        len(uid_to_wp), rel_ok, project_id,
    )


# ── Export ───────────────────────────────────────────────────────────────────

# (mpxj RelationType name, pred_is_from)
# pred_is_from=True  → _links.from is the predecessor, _links.to is the successor
# pred_is_from=False → _links.from is the successor,   _links.to is the predecessor
_OP_REL_CONFIG: dict[str, tuple[str, bool]] = {
    "precedes": ("FINISH_START", True),
    "follows":  ("FINISH_START", False),  # inverse of precedes
    "blocks":   ("FINISH_START", True),
    "relates":  ("FINISH_START", True),
}


def export_to_mspdi(project_id: int, client: OPClient) -> bytes:
    """Export an OpenProject project to MSPDI XML (openable by MS Project)."""
    _start_jvm()
    from jpype import JClass

    ProjectFile    = JClass("org.mpxj.ProjectFile")
    MSPDIWriter    = JClass("org.mpxj.mspdi.MSPDIWriter")
    ByteArrayOS    = JClass("java.io.ByteArrayOutputStream")
    LocalDateTime  = JClass("java.time.LocalDateTime")
    RelationType   = JClass("org.mpxj.RelationType")
    Duration       = JClass("org.mpxj.Duration")
    TimeUnit       = JClass("org.mpxj.TimeUnit")

    proj_info = client._get(f"/api/v3/projects/{project_id}")
    wps = client._get_all_work_packages(project_id)
    log.info("Export: %d work packages from '%s'", len(wps), proj_info.get("name"))

    pf = ProjectFile()
    pf.getProjectProperties().setName(proj_info.get("name", f"Project {project_id}"))

    wp_map = {wp["id"]: wp for wp in wps}
    task_map: dict[int, object] = {}

    def _depth(wp_id: int, seen: Optional[set] = None) -> int:
        if seen is None:
            seen = set()
        if wp_id in seen:
            return 0
        seen.add(wp_id)
        wp = wp_map.get(wp_id)
        if not wp:
            return 0
        href = ((wp.get("_links") or {}).get("parent") or {}).get("href") or ""
        if not href or "null" in href:
            return 0
        try:
            return 1 + _depth(int(href.rstrip("/").split("/")[-1]), seen)
        except Exception:
            return 0

    # ── Pass 1: build task tree ───────────────────────────────────────────────
    for wp in sorted(wps, key=lambda w: _depth(w["id"])):
        parent_href = ((wp.get("_links") or {}).get("parent") or {}).get("href") or ""
        parent_task = None
        if parent_href and "null" not in parent_href:
            try:
                pid = int(parent_href.rstrip("/").split("/")[-1])
                parent_task = task_map.get(pid)
            except Exception:
                pass

        task = parent_task.addTask() if parent_task else pf.addTask()
        task.setName(wp.get("subject") or f"WP#{wp['id']}")

        for date_str, hour in ((wp.get("startDate"), 8), (wp.get("dueDate"), 17)):
            if date_str:
                try:
                    y, m, d = date_str.split("-")
                    ldt = LocalDateTime.of(int(y), int(m), int(d), hour, 0)
                    if hour == 8:
                        task.setStart(ldt)
                    else:
                        task.setFinish(ldt)
                except Exception:
                    pass

        pct = wp.get("percentageDone", 0)
        if pct:
            task.setPercentageComplete(float(pct))

        desc = ((wp.get("description") or {}).get("raw") or "").strip()
        if desc:
            task.setNotes(desc)

        task_map[wp["id"]] = task

    # ── Pass 2: wire up predecessors ──────────────────────────────────────────
    wp_ids = [wp["id"] for wp in wps]
    relations = client.get_project_relations(wp_ids)
    log.info("Export: %d relations fetched", len(relations))

    rel_ok = rel_skip = 0
    for rel in relations:
        rel_type_str = (rel.get("type") or "precedes").lower()
        config = _OP_REL_CONFIG.get(rel_type_str)
        if config is None:
            rel_skip += 1
            continue

        rt_name, pred_is_from = config
        links = rel.get("_links") or {}
        from_href = (links.get("from") or {}).get("href") or ""
        to_href   = (links.get("to")   or {}).get("href") or ""

        try:
            from_id = int(from_href.rstrip("/").split("/")[-1])
            to_id   = int(to_href.rstrip("/").split("/")[-1])
        except (ValueError, IndexError):
            rel_skip += 1
            continue

        pred_id = from_id if pred_is_from else to_id
        succ_id = to_id   if pred_is_from else from_id
        pred_task = task_map.get(pred_id)
        succ_task = task_map.get(succ_id)
        if not pred_task or not succ_task:
            rel_skip += 1
            continue

        # OpenProject returns lag as "lag" (not "lagDays") in relation reads
        raw_lag  = rel.get("lag") or rel.get("lagDays") or 0
        try:
            lag_days = int(raw_lag)
        except (TypeError, ValueError):
            lag_days = 0
        lag_dur = Duration.getInstance(lag_days, TimeUnit.DAYS)
        rt      = RelationType.valueOf(rt_name)

        try:
            # Pass the Relation.Builder to addPredecessor — the method sets
            # successorTask=this internally before building the Relation object.
            # Calling .build() before passing it leaves successorTask null.
            Builder = JClass("org.mpxj.Relation$Builder")
            succ_task.addPredecessor(
                Builder().predecessorTask(pred_task).type(rt).lag(lag_dur)
            )
            rel_ok += 1
        except Exception as e:
            log.warning("Export: could not set predecessor WP#%d → WP#%d: %s",
                        pred_id, succ_id, e)
            rel_skip += 1

    log.info("Export: %d predecessors written, %d skipped", rel_ok, rel_skip)

    baos = ByteArrayOS()
    MSPDIWriter().write(pf, baos)
    return bytes(baos.toByteArray())


# ── CLI entry point ───────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import a Microsoft Project file (.mpp/.mspdi/.mpx) into OpenProject",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Import into an existing project (ID shown in the project URL)
  python msp_to_openproject.py project.mpp --project-id 5

  # Create a new project then import
  python msp_to_openproject.py project.mpp --create-project "Site Renovation 2025"

  # Override connection (default values come from OPENPROJECT_URL / OPENPROJECT_API_KEY)
  python msp_to_openproject.py project.mpp --project-id 5 \\
      --url https://op.example.com --api-key opapi-xxxx
        """,
    )
    parser.add_argument("mpp_file", help="Path to .mpp / .mspdi / .mpx file")

    dest = parser.add_mutually_exclusive_group(required=True)
    dest.add_argument("--project-id", type=int, metavar="ID",
                      help="Existing OpenProject project ID")
    dest.add_argument("--create-project", metavar="NAME",
                      help="Create a new project with this display name")

    parser.add_argument("--identifier", metavar="SLUG",
                        help="URL slug for --create-project (auto-derived if omitted)")
    parser.add_argument("--url",     default=_DEFAULT_URL, help="OpenProject base URL")
    parser.add_argument("--api-key", default=_DEFAULT_KEY, help="OpenProject API token")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug output")

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    if not args.url:
        sys.exit("Set OPENPROJECT_URL or pass --url")
    if not args.api_key:
        sys.exit("Set OPENPROJECT_API_KEY or pass --api-key")
    if not os.path.isfile(args.mpp_file):
        sys.exit(f"File not found: {args.mpp_file}")

    client = OPClient(args.url, args.api_key)

    if args.create_project:
        slug = (args.identifier or args.create_project.lower()[:40].replace(" ", "-"))
        log.info("Creating project '%s' (slug: %s) …", args.create_project, slug)
        proj = client.create_project(args.create_project, slug)
        project_id = int(proj["id"])
        log.info("Project created: ID=%d", project_id)
    else:
        project_id = args.project_id

    import_msp(args.mpp_file, project_id, client)


if __name__ == "__main__":
    main()
