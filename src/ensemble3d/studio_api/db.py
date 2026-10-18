"""SQLite persistence selected from the original Ensemble3D workbench.

The portfolio slice retains settings merges, project/version transactions and job
records. Engine installers, runtime configuration and unrelated tables are omitted.
See docs/PROVENANCE.md for the maintenance changes and original source hash.
"""
from __future__ import annotations
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

_UNSET = object()

class _ClosingConnection(sqlite3.Connection):
    """Connection that also closes itself when used as a context manager."""

    def __exit__(self, exc_type, exc, tb):  # type: ignore[override]
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


def utc_ts() -> str:
    # SQLite-friendly timestamp (UTC)
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def new_id(prefix: str = "p") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@dataclass
class ProjectRow:
    id: str
    created_at: str
    updated_at: str
    status: str
    title: str
    prompt: str
    style: str
    selected_concept_id: Optional[str]
    approved_image_path: Optional[str]
    approved_mask_path: Optional[str]
    reference_source_path: Optional[str]
    reference_meta_json: str
    config_json: str
    run_dir: str
    error: Optional[str]
    current_version_id: Optional[str] = None


class StudioDB:
    """Small local SQLite store; connections commit/rollback and close on exit."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init()


    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False, factory=_ClosingConnection)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        return conn


    def get_setting_json(self, key: str, default: Any) -> Any:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            if not row:
                return default
            try:
                return json.loads(row["value"])
            except Exception:
                return default


    def set_setting_json(self, key: str, value: Any) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )
            conn.commit()


    def merge_setting_json(
        self,
        key: str,
        *,
        updates: Dict[str, Any] | None = None,
        remove_keys: Iterable[str] = (),
    ) -> Dict[str, Any]:
        """Atomically merge dictionary updates into a JSON settings value."""
        updates = dict(updates or {})
        remove_keys = tuple(remove_keys or ())
        with self.connect() as conn:
            # Take a write lock before reading to avoid read-modify-write clobbering.
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            current: Dict[str, Any] = {}
            if row:
                try:
                    parsed = json.loads(row["value"])
                    if isinstance(parsed, dict):
                        current = dict(parsed)
                except Exception:
                    current = {}
            for rk in remove_keys:
                current.pop(str(rk), None)
            for uk, uv in updates.items():
                current[str(uk)] = uv
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(current)),
            )
            conn.commit()
            return current


    def recover_interrupted_jobs(self) -> int:
        """Mark in-process jobs left behind by a previous server exit as interrupted."""
        ts = utc_ts()
        with self.connect() as conn:
            rows = conn.execute("SELECT id FROM jobs WHERE status IN ('queued','running')").fetchall()
            count = len(rows)
            if count:
                conn.execute(
                    "UPDATE jobs SET status='error', stage='interrupted', message=?, error=?, updated_at=? "
                    "WHERE status IN ('queued','running')",
                    ("Interrupted when the server stopped", "Interrupted when the server stopped", ts),
                )
                conn.execute(
                    "UPDATE projects SET status='failed', error=?, updated_at=? WHERE status IN ('queued','running')",
                    ("The previous build was interrupted. Start it again when ready.", ts),
                )
                conn.commit()
            return count


    def create_project(self, *, title: str, prompt: str, style: str, run_dir: str, project_id: Optional[str] = None) -> ProjectRow:
        pid = project_id or new_id("proj")
        ts = utc_ts()
        default_cfg = {"engine_mode": "synthetic"}
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO projects(id, created_at, updated_at, status, title, prompt, style, config_json, run_dir)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (pid, ts, ts, "draft", title, prompt, style, json.dumps(default_cfg), run_dir),
            )
            conn.commit()

        return self.get_project(pid)


    def get_project(self, project_id: str) -> ProjectRow:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
            if not row:
                raise KeyError(project_id)
            return ProjectRow(**dict(row))


    def list_projects(self, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM projects ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (int(limit), int(offset)),
            ).fetchall()
            return [dict(r) for r in rows]


    def create_model_version(
        self,
        *,
        project_id: str,
        label: str,
        source_kind: str,
        glb_relpath: Optional[str] = None,
        stl_relpath: Optional[str] = None,
        obj_relpath: Optional[str] = None,
        ply_relpath: Optional[str] = None,
        preview_relpath: Optional[str] = None,
        parent_version_id: Optional[str] = None,
        source_ref: Optional[str] = None,
        units: Optional[str] = None,
        analysis: Optional[Dict[str, Any]] = None,
        operation: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        set_current: bool = True,
        version_id: Optional[str] = None,
        job_id: Optional[str] = None,
    ) -> str:
        vid = version_id or new_id("ver")
        ts = utc_ts()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if parent_version_id is not None:
                parent = conn.execute("SELECT project_id FROM model_versions WHERE id=?", (parent_version_id,)).fetchone()
                if parent is None or parent["project_id"] != project_id:
                    raise ValueError("Parent version must belong to this project")
            if job_id is not None:
                job = conn.execute("SELECT project_id, status FROM jobs WHERE id=?", (job_id,)).fetchone()
                if job is None or job["project_id"] != project_id or job["status"] != "running":
                    raise ValueError("Only this project's running job can publish a version")
            if set_current:
                conn.execute("UPDATE model_versions SET is_current=0 WHERE project_id=?", (project_id,))
            conn.execute(
                """
                INSERT INTO model_versions(
                    id, project_id, parent_version_id, created_at, label, source_kind, source_ref,
                    glb_relpath, stl_relpath, obj_relpath, ply_relpath, preview_relpath, units,
                    analysis_json, operation_json, metadata_json, is_current
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    vid,
                    project_id,
                    parent_version_id,
                    ts,
                    label,
                    source_kind,
                    source_ref,
                    glb_relpath,
                    stl_relpath,
                    obj_relpath,
                    ply_relpath,
                    preview_relpath,
                    units,
                    json.dumps(analysis or {}),
                    json.dumps(operation or {}),
                    json.dumps(metadata or {}),
                    1 if set_current else 0,
                ),
            )
            if set_current:
                conn.execute(
                    "UPDATE projects SET current_version_id=?, updated_at=? WHERE id=?",
                    (vid, ts, project_id),
                )
            if job_id is not None:
                conn.execute(
                    "UPDATE jobs SET status='done', progress=1, stage='completed', message='Completed', "
                    "result_json=?, error=NULL, updated_at=? WHERE id=?",
                    (json.dumps({"version_id": vid}), ts, job_id),
                )
            conn.commit()
        return vid


    @staticmethod
    def _decode_version_row(row: sqlite3.Row | Dict[str, Any]) -> Dict[str, Any]:
        item = dict(row)
        for src, dst in (
            ("analysis_json", "analysis"),
            ("operation_json", "operation"),
            ("metadata_json", "metadata"),
        ):
            try:
                item[dst] = json.loads(item.get(src) or "{}")
            except Exception:
                item[dst] = {}
        item["is_current"] = bool(item.get("is_current"))
        return item


    def get_model_version(self, version_id: str) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM model_versions WHERE id=?", (version_id,)).fetchone()
            return self._decode_version_row(row) if row else None


    def list_model_versions(self, project_id: str) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM model_versions WHERE project_id=? ORDER BY created_at DESC, id DESC",
                (project_id,),
            ).fetchall()
            return [self._decode_version_row(r) for r in rows]


    def set_current_version(self, project_id: str, version_id: str) -> None:
        ts = utc_ts()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT 1 FROM model_versions WHERE id=? AND project_id=?",
                (version_id, project_id),
            ).fetchone()
            if row is None:
                raise KeyError(version_id)
            conn.execute("UPDATE model_versions SET is_current=0 WHERE project_id=?", (project_id,))
            conn.execute("UPDATE model_versions SET is_current=1 WHERE id=?", (version_id,))
            conn.execute(
                "UPDATE projects SET current_version_id=?, updated_at=? WHERE id=?",
                (version_id, ts, project_id),
            )
            conn.commit()


    def create_job(
        self,
        *,
        job_id: str,
        project_id: Optional[str],
        kind: str,
        payload: Optional[Dict[str, Any]] = None,
        status: str = "queued",
    ) -> None:
        ts = utc_ts()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO jobs(
                    id, project_id, kind, status, progress, stage, message,
                    created_at, updated_at, payload_json, result_json, error
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (job_id, project_id, kind, status, 0.0, "queued", "Queued", ts, ts, json.dumps(payload or {}), "{}", None),
            )
            conn.commit()


    def update_job(
        self,
        job_id: str,
        *,
        status: Optional[str] = None,
        progress: Optional[float] = None,
        stage: Optional[str] = None,
        message: Optional[str] = None,
        result: Optional[Dict[str, Any]] = None,
        error: Optional[str] | object = _UNSET,
    ) -> None:
        fields: Dict[str, Any] = {"updated_at": utc_ts()}
        if status is not None:
            fields["status"] = status
        if progress is not None:
            fields["progress"] = float(max(0.0, min(1.0, progress)))
        if stage is not None:
            fields["stage"] = stage
        if message is not None:
            fields["message"] = message
        if result is not None:
            fields["result_json"] = json.dumps(result)
        if error is not _UNSET:
            fields["error"] = error
        sets = ", ".join([f"{k}=?" for k in fields])
        values = [fields[k] for k in fields]
        with self.connect() as conn:
            conn.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*values, job_id))
            conn.commit()


    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                return None
            item = dict(row)
            for key in ("payload_json", "result_json"):
                try:
                    item[key[:-5]] = json.loads(item.get(key) or "{}")
                except Exception:
                    item[key[:-5]] = {}
            return item


    def list_jobs(self, project_id: str, limit: int = 30) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE project_id=? ORDER BY created_at DESC LIMIT ?",
                (project_id, int(limit)),
            ).fetchall()
            out = []
            for row in rows:
                item = dict(row)
                try:
                    item["payload"] = json.loads(item.get("payload_json") or "{}")
                    item["result"] = json.loads(item.get("result_json") or "{}")
                except Exception:
                    item["payload"], item["result"] = {}, {}
                out.append(item)
            return out


    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript("""
CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    title TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    style TEXT NOT NULL,
                    selected_concept_id TEXT,
                    approved_image_path TEXT,
                    approved_mask_path TEXT,
                    reference_source_path TEXT,
                    reference_meta_json TEXT NOT NULL DEFAULT '{}',
                    config_json TEXT NOT NULL,
                    run_dir TEXT NOT NULL,
                    error TEXT,
                    current_version_id TEXT
                );
CREATE TABLE IF NOT EXISTS model_versions (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    parent_version_id TEXT,
                    created_at TEXT NOT NULL,
                    label TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    source_ref TEXT,
                    glb_relpath TEXT,
                    stl_relpath TEXT,
                    obj_relpath TEXT,
                    ply_relpath TEXT,
                    preview_relpath TEXT,
                    units TEXT,
                    analysis_json TEXT NOT NULL DEFAULT '{}',
                    operation_json TEXT NOT NULL DEFAULT '{}',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    is_current INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                );
CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    project_id TEXT,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    progress REAL NOT NULL DEFAULT 0,
                    stage TEXT,
                    message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    result_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT,
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                );
CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
            """)
            conn.commit()
