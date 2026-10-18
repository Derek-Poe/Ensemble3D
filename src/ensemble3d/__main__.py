import argparse
import json
from pathlib import Path

from .demo import crash_worker, run_demo
from .studio_api.db import StudioDB


def main():
    parser = argparse.ArgumentParser(description="Offline Ensemble3D job and immutable-asset demonstration")
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="Generate meshes/report and verify controlled crash recovery")
    demo.add_argument("--output", type=Path, default=Path("demo-output"))
    inspect = commands.add_parser("inspect", help="Read project summaries from an existing workspace")
    inspect.add_argument("workspace", type=Path)
    probe = commands.add_parser("_crash_worker", help=argparse.SUPPRESS)
    probe.add_argument("workspace", type=Path)
    probe.add_argument("project_id")
    probe.add_argument("marker", type=Path)
    args = parser.parse_args()
    if args.command == "demo":
        print(json.dumps(run_demo(args.output), indent=2))
    elif args.command == "inspect":
        database = args.workspace / "studio.db"
        if not database.is_file():
            parser.error("No studio.db in the supplied workspace")
        print(json.dumps(StudioDB(database).list_projects(), indent=2))
    else:
        crash_worker(args.workspace, args.project_id, args.marker)


if __name__ == "__main__":
    main()
