"""A focused synthetic vertical slice over the original Studio database model."""
from __future__ import annotations

from pathlib import Path
import time

from .assets import AssetStore, cube_glb, positive_size, scale_glb, unpack_fixture
from .studio_api.db import StudioDB
from .studio_api.jobs import JobManager


class Workspace:
    def __init__(self, root, *, max_workers=2, max_pending=4):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = StudioDB(self.root / "studio.db")
        self.assets = AssetStore(self.root / "assets")
        self.jobs = JobManager(self.db, max_workers=max_workers, max_pending=max_pending)

    def create_project(self, title="Synthetic mesh study"):
        if not isinstance(title, str) or not 1 <= len(title) <= 200:
            raise ValueError("Project title must contain 1..200 characters")
        return self.db.create_project(title=title, prompt="", style="synthetic", run_dir=".")

    def _draft(self, project_id, data, *, parent=None, operation=None):
        digest = self.assets.put(data)
        _, _, vertices = unpack_fixture(data)
        return {
            "project_id": project_id, "label": "Scaled cube" if parent else "Original synthetic cube",
            "source_kind": "operation" if parent else "synthetic", "parent_version_id": parent,
            "glb_relpath": f"assets/{digest}.glb", "units": "m",
            "analysis": {"vertices": len(vertices), "triangles": 12,
                         "bounds": [[min(v[i] for v in vertices) for i in range(3)],
                                    [max(v[i] for v in vertices) for i in range(3)]]},
            "operation": operation or {"kind": "generate_cube"},
            "metadata": {"sha256": digest, "engine": "synthetic_fixture_v1", "bytes": len(data)},
        }

    def generate(self, project_id, size=1.0):
        self.db.get_project(project_id)
        size = positive_size(size)
        return self.jobs.submit(project_id=project_id, kind="generate", payload={"size": size},
                                run=lambda _: self._draft(project_id, cube_glb(size)))

    def scale(self, project_id, parent_version_id, factor=1.5):
        factor = positive_size(factor)
        parent = self.db.get_model_version(parent_version_id)
        if parent is None or parent["project_id"] != project_id:
            raise ValueError("Parent version must belong to this project")
        digest = parent["metadata"]["sha256"]
        return self.jobs.submit(project_id=project_id, kind="scale",
            payload={"parent_version_id": parent_version_id, "factor": factor},
            run=lambda _: self._draft(project_id, scale_glb(self.assets.read(digest), factor),
                parent=parent_version_id, operation={"kind": "scale", "factor": factor}))

    def wait(self, job_id, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            if job["status"] in ("done", "error"):
                return job
            time.sleep(0.01)
        raise TimeoutError("Local job did not finish before the wait deadline")

    def close(self):
        self.jobs.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
