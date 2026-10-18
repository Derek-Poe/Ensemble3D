"""Generate a small offline report, including a controlled child-process crash."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import html
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

from .assets import cube_glb, unpack_fixture
from .workspace import Workspace


def crash_worker(root, project_id, marker):
    """Owned test helper: leave one running and one queued job for recovery."""
    with Workspace(root, max_workers=1, max_pending=1) as workspace:
        started = threading.Event()
        stop = threading.Event()

        def interrupted(_):
            # Simulate an engine finishing its file before the DB publication.
            workspace.assets.put(cube_glb(0.75))
            started.set()
            stop.wait()

        active = workspace.jobs.submit(project_id=project_id, kind="crash_probe", payload={}, run=interrupted)
        if not started.wait(10):
            raise TimeoutError("Crash probe did not start")
        queued = workspace.jobs.submit(project_id=project_id, kind="queued_probe", payload={}, run=lambda _: None)
        Path(marker).write_text(json.dumps({"running": active, "queued": queued}), encoding="utf-8")
        stop.wait()


def crash_and_recover(root, project_id, *, timeout=15):
    root = Path(root)
    marker = root / "crash-ready.json"
    if marker.exists():
        raise ValueError("Crash marker already exists; use a fresh demo directory")
    process = subprocess.Popen([sys.executable, "-m", "ensemble3d", "_crash_worker", str(root),
                                project_id, str(marker)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("Owned crash probe exited before becoming ready")
            if marker.exists():
                try:
                    ids = json.loads(marker.read_text(encoding="utf-8"))
                    break
                except json.JSONDecodeError:
                    pass
            time.sleep(0.02)
        else:
            raise TimeoutError("Owned crash probe did not become ready")
        process.kill()  # Only the exact child started above; no process-name matching.
        process.wait(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        process.stderr.close()
    with Workspace(root) as recovered:
        jobs = [recovered.jobs.get(jid) for jid in ids.values()]
        if recovered.jobs.recovered_jobs != 2 or any(job["stage"] != "interrupted" for job in jobs):
            raise AssertionError("Both abandoned jobs must become interrupted errors")
        return jobs


def mesh_svg(data, scale):
    _, _, vertices = unpack_fixture(data)
    points = [(160 + (x - z) * scale, 125 + (x + z) * scale * 0.4 - y * scale)
              for x, y, z in vertices]
    faces = [([4, 5, 6, 7], "#0f3f61"), ([1, 5, 6, 2], "#187a99"), ([3, 2, 6, 7], "#41b4ce")]
    shapes = []
    for face, color in faces:
        coordinates = " ".join(f"{points[i][0]:.2f},{points[i][1]:.2f}" for i in face)
        shapes.append(f'<polygon points="{coordinates}" fill="{color}" stroke="#95e2f2" stroke-width="1.5"/>')
    return '<svg viewBox="0 0 320 240" role="img" aria-label="Projection of the stored synthetic cube">' + "".join(shapes) + '</svg>'


def write_report(output, report, assets):
    cards = []
    for version in report["versions"]:
        digest = version["sha256"]
        cards.append(f'<article><h2>{html.escape(version["label"])}</h2>{mesh_svg(assets[digest], 70)}'
                     f'<p>{version["edge_length_m"]:g} m edge · 12 triangles</p>'
                     f'<code>{digest[:16]}…</code><p><a href="{version["file"]}">Download GLB</a></p></article>')
    rows = "".join(f'<tr><td>{html.escape(job["kind"])}</td><td>{html.escape(job["status"])}</td>'
                   f'<td>{html.escape(job["stage"])}</td></tr>' for job in report["jobs"])
    document = f'''<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Ensemble3D · Local job study</title>
<style>body{{margin:0;background:#101923;color:#e4eef4;font:16px system-ui,sans-serif}}main{{max-width:1000px;margin:40px auto;padding:0 24px}}h1{{font-size:38px;margin:8px 0}}.eyebrow{{color:#80d9e6;letter-spacing:.14em;font-size:12px}}.sub{{color:#b0c3d1;max-width:760px;line-height:1.6}}.cards{{display:grid;grid-template-columns:1fr 1fr;gap:24px;margin:30px 0}}article,section{{background:#192735;border:1px solid #314455;border-radius:12px;padding:22px}}h2{{font-size:20px;margin:0}}svg{{width:100%;max-height:240px}}a{{color:#80d9e6}}code{{color:#aac1d1;font-size:12px}}table{{width:100%;border-collapse:collapse;margin-top:16px}}td,th{{text-align:left;padding:12px 0;border-bottom:1px solid #314455}}.pass{{color:#a4dfb7}}footer{{color:#aabcca;font-size:13px;line-height:1.6;margin:24px 0}}@media(max-width:650px){{.cards{{grid-template-columns:1fr}}}}</style>
<main><div class="eyebrow">ENSEMBLE3D / REPRODUCIBLE LOCAL WORKFLOW</div><h1>Versioned assets. Recoverable jobs.</h1>
<p class="sub">A deterministic cube generator exercises the orchestration path. A scale operation creates a new immutable asset; a controlled child-process crash leaves two jobs for explicit restart recovery.</p>
<div class="cards">{"".join(cards)}</div><section><h2>Persistent job journal</h2><table><tr><th>Operation</th><th>State</th><th>Stage</th></tr>{rows}</table>
<p class="pass">Original bytes preserved · one current version · both abandoned jobs recovered</p></section>
<footer>Recorded {html.escape(report["recorded_utc"])}. The meshes and this SVG projection are synthetic. No generative model, device, network service or performance benchmark is involved. Local process ownership is enforced with an OS file lock; this is not a distributed queue.</footer></main></html>'''
    (output / "report.html").write_text(document, encoding="utf-8")
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


def run_demo(output):
    output = Path(output).resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError("Demo output must be absent or empty; existing work is preserved")
    output.mkdir(parents=True, exist_ok=True)
    root = output / "state"
    with Workspace(root) as workspace:
        project = workspace.create_project()
        first = workspace.wait(workspace.generate(project.id, size=1.0))
        if first["status"] != "done":
            raise RuntimeError("Synthetic generation failed")
        original = workspace.db.get_model_version(first["result"]["version_id"])
        original_bytes = workspace.assets.read(original["metadata"]["sha256"])
        second = workspace.wait(workspace.scale(project.id, original["id"], factor=1.5))
        if second["status"] != "done":
            raise RuntimeError("Synthetic scaling failed")
        scaled = workspace.db.get_model_version(second["result"]["version_id"])
        if workspace.assets.read(original["metadata"]["sha256"]) != original_bytes:
            raise AssertionError("Operation changed the original asset")
    recovered_jobs = crash_and_recover(root, project.id)
    with Workspace(root) as workspace:
        versions = workspace.db.list_model_versions(project.id)
        if len(versions) != 2 or sum(version["is_current"] for version in versions) != 1:
            raise AssertionError("Restart changed committed version history")
        if workspace.db.get_project(project.id).current_version_id != scaled["id"]:
            raise AssertionError("Restart changed the current version")
        assets = {}
        summaries = []
        for name, version in (("original.glb", original), ("scaled.glb", scaled)):
            digest = version["metadata"]["sha256"]
            data = workspace.assets.read(digest)
            assets[digest] = data
            (output / name).write_bytes(data)
            summaries.append({"label": version["label"], "sha256": digest, "file": name,
                              "edge_length_m": version["analysis"]["bounds"][1][0] * 2,
                              "version_id": version["id"], "parent_version_id": version["parent_version_id"]})
        report = {"recorded_utc": datetime.now(timezone.utc).isoformat(), "synthetic": True,
                  "workers": 2, "max_pending": 4, "original_preserved": hashlib.sha256(original_bytes).hexdigest() == original["metadata"]["sha256"],
                  "versions": summaries, "current_version_id": scaled["id"], "recovered_jobs": 2,
                  "jobs": [{key: job[key] for key in ("id", "kind", "status", "stage")}
                           for job in (workspace.db.get_job(jid) for jid in
                               [first["id"], second["id"], *(job["id"] for job in recovered_jobs)])]}
        write_report(output, report, assets)
    return report
