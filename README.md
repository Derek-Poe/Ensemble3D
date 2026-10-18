# Ensemble3D · Local jobs and versioned assets

An offline slice of Ensemble3D Studio focused on SQLite transaction correctness, bounded background work, immutable mesh versions and recovery after a process stops. A deterministic cube generator stands in for the original external generation engines.

Recovery means **detecting interrupted work and preserving completed versions**. Jobs are not automatically resumed or replayed. Execution stays in one local Python process; this is not a distributed queue or a generative-model implementation.

## Run the complete demonstration

Requires Python 3.11 or newer. There are no third-party runtime dependencies.

```sh
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# PowerShell: .venv\Scripts\Activate.ps1
python -m pip install .
python -m unittest discover -s tests -v
python -m ensemble3d demo --output demo-output
```

Open `demo-output/report.html` in a browser. The command:

1. Generates a tiny GLB cube and commits its original version.
2. Scales its vertices into a new GLB, preserving the original bytes and parent link.
3. Starts an owned helper with one running and one queued job, then deliberately terminates that exact child process.
4. Reopens the workspace, marks both abandoned jobs interrupted, and verifies that the two completed versions and current selection survived.
5. Exports the meshes, JSON evidence and an HTML report with SVG projections of the stored geometry.

The output directory must be absent or empty; the command refuses to overwrite existing work. It downloads no models, runs no inference and contacts no services. Build isolation may download the pinned packaging tools when installing from source.

## What the implementation demonstrates

| Mechanism | Implementation |
| --- | --- |
| No lost updates when threads merge independent JSON keys | [`merge_setting_json`](src/ensemble3d/studio_api/db.py) obtains `BEGIN IMMEDIATE` before reading |
| Consistent current-version selection | Version insertion and project-pointer changes share one SQLite transaction |
| Successful job linked to its committed version | The version, current pointer and completed job record commit together |
| Bounded admission | [`JobManager`](src/ensemble3d/studio_api/jobs.py) allows two workers plus four pending slots by default; excess submissions fail immediately |
| Controlled shutdown | New submissions stop; running work drains; waiting work can optionally be cancelled |
| Restart ownership | An OS file lock excludes another manager before interrupted jobs are recovered |
| Original asset preservation | [`AssetStore`](src/ensemble3d/assets.py) writes content-addressed GLB files before publishing database references |

The current version is the **last committed selection**, not necessarily the last submitted job. Concurrent work can finish in a different order. Previous versions remain addressable.

## Using the local API

```python
from ensemble3d.workspace import Workspace

with Workspace("demo-output/workspace", max_workers=2, max_pending=4) as studio:
    project = studio.create_project("Synthetic cube study")
    original_job = studio.wait(studio.generate(project.id, size=1.0))
    original = original_job["result"]["version_id"]
    scaled_job = studio.wait(studio.scale(project.id, original, factor=1.5))
    print(scaled_job["status"], scaled_job["result"])
```

Dimensions use glTF metres. Scaling supports only this package's deliberately small synthetic fixture layout. It is not a general mesh importer, validator or CAD transformation library. `wait` has a deadline; it does not cancel the worker on timeout. `close()` waits for running handlers, so custom handlers must have their own finite/cooperative execution bound.

## Scope and evidence

The original Studio included a browser workbench, model-engine adapters/installers, concept selection and broader mesh operations. Those components, downloaded engines, model weights, user outputs and runtime configuration are excluded from this focused package. See [provenance](docs/PROVENANCE.md) for original-derived code versus maintenance additions, and [architecture](docs/ARCHITECTURE.md) for crash boundaries and limitations.

The tests exercise concurrent settings/version updates, rejected admission, duplicate IDs, exceptional handlers, ownership, cancellation, asset integrity and a real killed-child restart. CI installs the package and runs the same suite and synthetic demonstration on Linux and Windows. Current recorded results are in [demo evidence](docs/DEMO.md).

MIT License — Derek Poe. [Third-party notices](THIRD_PARTY_NOTICES.md).
