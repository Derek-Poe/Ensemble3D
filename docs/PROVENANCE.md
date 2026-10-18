# Original implementation and maintenance

This package is a focused source extraction from Derek Poe's local Ensemble3D Studio workbench. The original `.git` directory contained no recoverable repository history. No history or original release commit is invented here. Private source snapshots and exact hashes were retained separately; the source and its runtime directories were not changed.

## Original-derived core

`src/ensemble3d/studio_api/db.py` selects the original connection lifecycle, project rows, atomic JSON settings merge, model-version/current-selection transactions, job records and interrupted-job recovery. Eighteen original methods were selected rather than copying the entire application. The original concurrent-settings regression informed the retained test case.

Original source hashes (SHA-256):

| Source-relative path | Hash |
| --- | --- |
| `ensemble3d/studio_api/db.py` | `694a4fd44f5bfcecede1ef4d3d524724ecdcda87ecf430eafb068c65e59fa4fe` |
| `ensemble3d/studio_api/jobs.py` | `96a4387221003f252eacdb02d81746d8ba73b7760e295d9752cb365647a8a3f0` |
| `tests/test_installer_reliability.py` | `6fff2b29490f2c0599eac1ac94f78b32423c279f8846796ce5d13f8ca945d335` |
| `tests/test_workbench_product.py` | `b21a9ded415ee9bbda14cfef0d5126259be4ac7ccec9d226eb231c7fdf95a9d2` |

The original `JobManager` already used a lock and a bounded number of executor workers. The original database already marked abandoned jobs interrupted. The package does not attribute those ideas to the maintenance work.

## Maintenance added for this package

- Reduced the schema/application scope to projects, settings, versions and jobs; provided a fresh-workspace package instead of copying production state or migrations for unrelated features.
- Added bounded pending-job admission, integer capacity validation, duplicate-ID protection, terminal exceptional states, shutdown and local process ownership.
- Joined version publication and successful job completion in one transaction and validated parent/job project ownership.
- Added immutable hash-addressed files, deterministic cube generation/scaling, the report and a controlled child-process crash demonstration.
- Added focused concurrency, failure and packaging checks, installation metadata, CI, documentation and the owner-approved MIT license.

The small synthetic engine, report and stronger job lifecycle are maintenance scaffolding. The original contribution being demonstrated is local orchestration, database concurrency and versioned-asset management. External generation engines perform the original application's generation work; this package claims no model training or generative-model research.

The full Studio UI, third-party engine adapters/installers, downloaded models, historical benchmarks and user-generated assets are not redistributed here. The selected original test files were used as references; they are not presented as a freshly passing full-Studio suite.
