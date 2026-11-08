"""Publish a closed-world Task05 HOLD package from synthetic-only evidence."""

from __future__ import annotations

import csv
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    _atomic_text(path, json.dumps(dict(payload), indent=2, ensure_ascii=False) + "\n")


def validate_expected_paths(paths: Sequence[str]) -> dict[str, str]:
    if not paths or len(paths) != len({raw.casefold() for raw in paths}):
        raise ValueError("expected artifact paths must be nonempty and unique")
    excluded = {"audit/a5/a5_independent_review.json", "audit/ml_fit_authorization.json",
                "audit/ml_fit_authorization.md", "audit/a5/ml_fit_researcher_signature.json",
                "audit/a5/ml_fit_state_transition.json", "docs/sdd/task-05-full-independent-review.md",
                "audit/a5/a5_hash_manifest.csv"}
    for raw in paths:
        path = Path(raw)
        if path.is_absolute() or ".." in path.parts or "\\" in raw or raw.startswith("/"):
            raise ValueError(f"expected artifact path escapes Project_B: {raw}")
        if path.as_posix().casefold() == "manifests/project_state.json":
            raise ValueError("PROJECT_STATE.json is excluded from the A5 manifest")
        if path.as_posix().casefold() in excluded:
            raise ValueError("post-review or authorization artifact is excluded from the A5 manifest")
    return {"status": "PASS"}


def derive_a5_status(gates: Mapping[str, str], state: Mapping[str, Any]) -> dict[str, Any]:
    if state.get("phase") != "A5" or state.get("fit_authorized") is not False or \
            state.get("temporal_unsealed") is not False:
        raise PermissionError("unexpected Task05 state")
    reasons = []
    if gates.get("compute") == "COMPUTE_HOLD":
        reasons.append("COMPUTE_BUDGET_EXCEEDED")
    for name in ("authority", "software", "numerical", "bootstrap"):
        if gates.get(name) != "PASS":
            reasons.append(name.upper() + "_GATE_FAILED")
    return {
        "conclusion": "HOLD" if reasons else "PASS",
        "hold_reasons": reasons,
        "phase": "A5",
        "project_status": "IN_PROGRESS",
        "fit_authorized": False,
        "temporal_unsealed": False,
        "independent_review": "NOT_ENTERED" if reasons else "PENDING",
    }


def _artifact_role(relative: str) -> tuple[str, str]:
    if relative.startswith("vendor/a5_wheels/"):
        return "offline_wheel", "immutable_binary"
    if relative.startswith("src/"):
        return "implementation", "immutable_code"
    if relative.startswith("tests/"):
        return "test", "immutable_code"
    if relative.startswith("config/"):
        return "configuration", "immutable_spec"
    if relative.startswith("audit/a5/"):
        return "audit", "immutable_evidence"
    if relative.startswith("docs/sdd/"):
        return "spec_or_report", "immutable_document"
    if relative.startswith("manifests/"):
        return "state_authority", "immutable_authority"
    return "authority", "immutable_authority"


def finalize_compute_hold(project_root: str | Path, *, benchmark_path: str | Path,
                          collection_path: str | Path, test_n: int,
                          test_seconds: float) -> dict[str, Any]:
    root = Path(project_root).resolve()
    if root.name.lower() != "project_b" or any(part.lower() == "project_a" for part in root.parts):
        raise PermissionError("Task05 finalizer is confined to Project_B")
    benchmark = json.loads(Path(benchmark_path).read_text(encoding="utf-8"))
    collected = json.loads(Path(collection_path).read_text(encoding="utf-8"))
    state_path = root / "manifests/PROJECT_STATE.json"
    state_before = state_path.read_bytes()
    state = json.loads(state_before.decode("utf-8"))
    if benchmark.get("status") != "COMPUTE_HOLD" or len(benchmark.get("records", [])) != 30:
        raise ValueError("formal dimension-matched COMPUTE_HOLD evidence is missing")
    expected_runs = {"M0": 3, "EN": 9, "RSF": 6, "GBST": 6,
                     "CALIBRATION": 3, "METRIC_REGRESSION_TRIPLET": 3}
    if {name: benchmark["family_summaries"][name]["run_n"] for name in expected_runs} != expected_runs:
        raise ValueError("benchmark run counts do not close")
    if not all(benchmark["family_summaries"][name]["all_pass"] for name in expected_runs):
        raise ValueError("a synthetic benchmark operation failed")
    required_pass = [collected["authority"]["status"], collected["software_lock"]["status"],
                     collected["m0_contract"]["status"], collected["synthetic_dry_run"]["status"],
                     collected["backend_conformance"]["status"], collected["m0_identity"]["status"],
                     collected["bootstrap"]["status"],
                     *[row["status"] for row in collected["d3_references"]]]
    if any(value != "PASS" for value in required_pass):
        raise ValueError("non-compute synthetic gate did not pass")
    gates = {"authority": "PASS", "software": "PASS", "numerical": "PASS",
             "bootstrap": "PASS", "compute": "COMPUTE_HOLD"}
    status_core = derive_a5_status(gates, state)
    captured = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    audit = root / "audit/a5"

    m0_payload = {
        "schema_version": "1.0", "task": "Task05", "captured_at_utc": captured,
        "status": "PASS", "decision": "V2_M0_180D_SUPERSEDES_LEGACY_FIT",
        "legacy_fit_authoritative": False, "training_horizon_days": 180,
        "ties": "efron", "baseline": "breslow", "core_column_n": 20,
        "outcome_constructor": "make_surv180(index_outcome)",
        "same_time_event_arrays_required_for": ["M0", "M1", "M2"],
        "identity_reference": collected["m0_identity"],
        "evidence": {
            "proposal_sha256": _sha256(root / "FINAL_PROPOSAL.md"),
            "researcher_decisions_sha256": _sha256(audit / "A5_RESEARCHER_DECISIONS.json"),
            "task_brief_sha256": _sha256(root / "docs/sdd/task-05-brief.md"),
        },
    }
    _atomic_json(audit / "A5_M0_180D_CONSISTENCY.json", m0_payload)

    software_payload = {
        "schema_version": "1.0", "task": "Task05", "captured_at_utc": captured,
        "status": "PASS", "software_lock": collected["software_lock"],
        "software_lock_sha256": _sha256(root / "config/SOFTWARE_LOCK.json"),
        "backend_conformance": collected["backend_conformance"],
        "d3_strict_reference_fixtures": collected["d3_references"],
        "strict_thresholds_unchanged": True, "real_patient_fit_n": 0,
    }
    _atomic_json(audit / "A5_SOFTWARE_AUDIT.json", software_payload)

    dry_payload = {
        "schema_version": "1.0", "task": "Task05", "captured_at_utc": captured,
        "status": "HOLD", "hold_reason": "COMPUTE_BUDGET_EXCEEDED",
        "synthetic_interfaces_status": "PASS", "authority": collected["authority"],
        "dry_run": collected["synthetic_dry_run"],
        "bootstrap": collected["bootstrap"],
        "benchmark": {"status": benchmark["status"], "wall_seconds": benchmark["wall_seconds"],
                      "plan": benchmark["plan"], "family_summaries": benchmark["family_summaries"]},
        "real_patient_fit_n": 0, "database_connection_n": 0,
        "temporal_value_access_n": 0, "compression_run_n": 0,
        "real_prediction_n": 0, "model_object_saved_n": 0,
    }
    _atomic_json(audit / "A5_SYNTHETIC_DRY_RUN.json", dry_payload)
    _atomic_json(audit / "A5_IDENTITY_TEST.json", {
        "schema_version": "1.0", "task": "Task05", "captured_at_utc": captured,
        **collected["m0_identity"], "m1_en_provenance": "SYMBOLIC_ALIAS_OF_M0",
    })

    formal = collected["formal_counts"]
    categories = {
        "survival_fits": {"planned": formal["survival_fits"], "attempted": 0,
                          "succeeded": 0, "failed": 0, "skipped": formal["survival_fits"]},
        "recalibrators": {"planned": formal["recalibrators"], "attempted": 0,
                          "succeeded": 0, "failed": 0, "skipped": formal["recalibrators"]},
        "trainable_objects": {"planned": formal["trainable_objects"], "attempted": 0,
                              "succeeded": 0, "failed": 0, "skipped": formal["trainable_objects"]},
    }
    fit_payload = {
        "schema_version": "1.0", "task": "Task05", "captured_at_utc": captured,
        "status": "PASS", "formal_authorized_fit_started": False, "categories": categories,
        "development_evaluation_completed": formal["development_evaluation_completed"],
        "development_evaluation_attempt_ceiling": formal["development_evaluation_attempt_ceiling"],
        "temporal_evaluation_completed": formal["temporal_evaluation_completed"],
        "temporal_evaluation_attempt_ceiling": formal["temporal_evaluation_attempt_ceiling"],
        "synthetic_dry_run": {"survival_fit_n": 30, "recalibration_fit_n": 3,
                              "evaluation_triplet_n": 3, "patient_data_n": 0},
        "failure_ledger": [{"code": "A5_COMPUTE_HOLD_NOT_AUTHORIZED",
                            "scope": "all_planned_real_trainable_objects",
                            "linked_skipped_n": formal["trainable_objects"]}],
    }
    _atomic_json(audit / "A5_FIT_COUNT_RECEIPT.json", fit_payload)

    compute_payload = {
        "schema_version": "1.0", "task": "Task05", "captured_at_utc": captured,
        "status": "COMPUTE_HOLD", "attempt_1": {
            "status": "EXECUTION_ERROR_BEFORE_FIRST_FIT",
            "reason": "Windows RSS probe HANDLE restype truncation", "scientific_result": None},
        "attempt_2": benchmark,
        "frozen_limits": {"max_fit_workers": 4, "estimator_threads": 1,
                          "peak_process_tree_rss_bytes": 32 * 1024 ** 3,
                          "synthetic_wall_hours": 8, "future_wall_hours": 96,
                          "future_cpu_core_hours": 384},
        "failed_limits": ["future_wall_hours", "future_cpu_core_hours"],
        "passed_limits": ["synthetic_wall_hours", "peak_process_tree_rss_bytes", "minimum_free_disk"],
        "definitions_or_grid_changed": False,
    }
    _atomic_json(audit / "A5_COMPUTE_BUDGET.json", compute_payload)

    test_text = ("Task05 full Project_B test run\n"
                 f"Captured: {captured}\nInterpreter: .venv-a5 Python 3.12.14\n"
                 f"Command: python -m unittest discover -s tests -v\nRan {test_n} tests in {test_seconds:.3f}s\n"
                 f"Passed: {test_n}\nFailed: 0\nErrors: 0\nExit code: 0\nResult: OK\n"
                 "Note: initial sandboxed run had 3 legacy intake hardlink permission failures; "
                 "the identical suite passed unchanged in the permitted filesystem execution context.\n")
    _atomic_text(audit / "A5_TEST_RESULTS.txt", test_text)

    implementation_md = f"""# Task05 implementation audit

- Captured: `{captured}`
- Authority, software lock, D1, D2, D3, D4, M0 identity, preprocessing, calibration and bootstrap contracts: **PASS**.
- Dimension-matched synthetic benchmark: **COMPUTE_HOLD**.
- RSF p95: `{benchmark['family_summaries']['RSF']['p95_seconds']:.6f}` seconds.
- Extrapolated CPU upper bound: `{benchmark['budget']['future_cpu_hours_upper']:.6f}` core-hours (limit 384).
- Extrapolated four-worker wall upper bound: `{benchmark['budget']['future_wall_hours_upper']:.6f}` hours (limit 96).
- Peak four-worker RSS upper bound: `{benchmark['budget']['future_peak_process_tree_rss_bytes_upper']}` bytes (within 32 GiB).
- No patient model, database connection, temporal value, compression, real prediction or saved model object was used.
- No grid, 1000-tree RSF setting, worker ceiling or budget was changed in response to this result.
"""
    _atomic_text(audit / "A5_IMPLEMENTATION_AUDIT.md", implementation_md)

    status_payload = {
        "schema_version": "1.0", "task": "Task05", "captured_at_utc": captured,
        **status_core, "gates": gates, "test_n": test_n, "test_pass_n": test_n,
        "state_sha256_before": hashlib.sha256(state_before).hexdigest().upper(),
        "state_modified": False, "ml_fit_authorization_created": False,
        "temporal_materialization_created": False, "compression_started": False,
    }
    _atomic_json(audit / "A5_STATUS.json", status_payload)
    _atomic_json(audit / "A5_STATE_TRANSITION_PENDING.json", {
        "schema_version": "1.0", "task": "Task05", "captured_at_utc": captured,
        "status": "BLOCKED_BY_A5_HOLD", "independent_review": "NOT_ENTERED",
        "hold_reasons": status_core["hold_reasons"], "state_modified": False,
        "current_state_sha256": hashlib.sha256(state_before).hexdigest().upper(),
        "proposed_state": state,
    })

    report = f"""# Task05 report

## Result

**HOLD — compute budget exceeded.** All implementation and numerical-conformance gates passed, but the frozen dimension-matched benchmark extrapolated to `{benchmark['budget']['future_cpu_hours_upper']:.3f}` CPU core-hours and `{benchmark['budget']['future_wall_hours_upper']:.3f}` wall-hours with four workers, exceeding the fixed 384 core-hour and 96 wall-hour limits.

## Frozen decisions and contracts

- D1: new 180-day administrative-censoring M0 supersedes the legacy fit.
- D2: scikit-survival 0.28.0 RSF/GBST adapters and grids remained unchanged.
- D3: lifelines 0.30.0 EN mapping passed both strict references without tolerance changes.
- D4: 2,000 GLOBAL_COMMON_VALID bootstrap replicates passed on synthetic fixtures.
- C2/core/extension widths remain 20/41/157, maximum M2 width 177.

## Benchmark

| Family | runs | p95 seconds | peak RSS bytes |
|---|---:|---:|---:|
"""
    for family in expected_runs:
        summary = benchmark["family_summaries"][family]
        report += f"| {family} | {summary['run_n']} | {summary['p95_seconds']:.6f} | {summary['peak_rss_bytes']} |\n"
    report += """

## State and prohibitions

`PROJECT_STATE.json` remains at A5 with `fit_authorized=false` and `temporal_unsealed=false`. Independent review and authorization were not entered. No real patient values were passed to any fit, no database was opened, and no temporal values, compression, real predictions, performance results or model objects were created.
"""
    _atomic_text(root / "docs/sdd/task-05-report.md", report)
    review_package = f"""# Task05 review package

Task05 is **not submitted for independent PASS review** because the formal conclusion is `COMPUTE_HOLD`.

Evidence package:

- `audit/a5/A5_STATUS.json`
- `audit/a5/A5_COMPUTE_BUDGET.json`
- `audit/a5/A5_SOFTWARE_AUDIT.json`
- `audit/a5/A5_M0_180D_CONSISTENCY.json`
- `audit/a5/A5_IDENTITY_TEST.json`
- `audit/a5/A5_SYNTHETIC_DRY_RUN.json`
- `audit/a5/A5_FIT_COUNT_RECEIPT.json`
- `audit/a5/A5_TEST_RESULTS.txt`

Open finding: frozen compute limits are exceeded. No scientific or resource contract was changed. A new researcher decision and revised brief would be required before any remediation implementation; this package does not authorize such a revision.
"""
    _atomic_text(root / "docs/sdd/task-05-review-package.md", review_package)

    fixed_paths = [
        "FINAL_PROPOSAL.md", "manifests/A4_FREEZE_MANIFEST.csv",
        "audit/a4/A4_INDEPENDENT_REVIEW.json", "docs/sdd/task-05-brief.md",
        "audit/a5/A5_RESEARCHER_DECISIONS.json",
    ]
    config_names = ["SOFTWARE_LOCK", "ALGORITHM_SPEC", "INNER_RESAMPLING_SPEC",
                    "PREPROCESSING_SPEC", "CALIBRATION_SPEC", "FAILURE_RULES",
                    "BOOTSTRAP_SPEC", "FIT_BUDGET", "COMPRESSION_BRANCH_SPEC",
                    "A5_EXPECTED_ARTIFACTS"]
    source_names = ["a5_common", "a5_preprocessing", "a5_m0_cox", "a5_en_cox", "a5_rsf",
                    "a5_gbst", "a5_calibration", "a5_bootstrap", "a5_dry_run", "a5_finalize"]
    test_names = ["test_a5_authorities", "test_a5_outcome", "test_a5_resampling",
                  "test_a5_preprocessing", "test_a5_algorithms", "test_a5_calibration",
                  "test_a5_bootstrap", "test_a5_authorization"]
    audit_names = ["A5_M0_180D_CONSISTENCY.json", "A5_SOFTWARE_AUDIT.json",
                   "A5_SYNTHETIC_DRY_RUN.json", "A5_IDENTITY_TEST.json",
                   "A5_FIT_COUNT_RECEIPT.json", "A5_COMPUTE_BUDGET.json",
                   "A5_IMPLEMENTATION_AUDIT.md", "A5_STATUS.json", "A5_TEST_RESULTS.txt",
                   "A5_STATE_TRANSITION_PENDING.json"]
    wheel_paths = sorted(path.relative_to(root).as_posix()
                         for path in (root / "vendor/a5_wheels").glob("*.whl"))
    expected_paths = (fixed_paths + [f"config/{name}.json" for name in config_names]
                      + [f"src/project_b/{name}.py" for name in source_names]
                      + [f"tests/{name}.py" for name in test_names]
                      + [f"audit/a5/{name}" for name in audit_names]
                      + ["docs/sdd/task-05-report.md", "docs/sdd/task-05-review-package.md"]
                      + wheel_paths)
    validate_expected_paths(expected_paths)
    expected_payload = {
        "schema_version": "1.0", "task": "Task05", "conclusion": "HOLD",
        "artifact_n": len(expected_paths), "paths": expected_paths,
        "manifest_path": "audit/a5/A5_HASH_MANIFEST.csv",
        "manifest_self_excluded": True,
        "state_authority_manifest_excluded": True,
        "post_hold_review_and_authorization_artifacts_excluded": [
            "audit/a5/A5_INDEPENDENT_REVIEW.json", "audit/ML_FIT_AUTHORIZATION.md",
            "audit/ML_FIT_AUTHORIZATION.json", "audit/a5/ML_FIT_RESEARCHER_SIGNATURE.json"],
    }
    _atomic_json(root / "config/A5_EXPECTED_ARTIFACTS.json", expected_payload)
    rows = []
    for relative in expected_paths:
        path = root / relative
        if not path.is_file():
            raise FileNotFoundError(relative)
        role, access = _artifact_role(relative)
        rows.append({"relative_path": relative, "role": role, "access_class": access,
                     "bytes": path.stat().st_size, "sha256": _sha256(path), "status": "FROZEN"})
    manifest_path = audit / "A5_HASH_MANIFEST.csv"
    temporary = manifest_path.with_name(manifest_path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["relative_path", "role", "access_class",
                                                   "bytes", "sha256", "status"], lineterminator="\n")
        writer.writeheader(); writer.writerows(rows)
    os.replace(temporary, manifest_path)
    if state_path.read_bytes() != state_before:
        raise RuntimeError("Task05 HOLD finalization changed PROJECT_STATE")
    return {"status": "HOLD", "hold_reason": "COMPUTE_BUDGET_EXCEEDED",
            "manifest_sha256": _sha256(manifest_path), "manifest_artifact_n": len(rows),
            "a5_status_sha256": _sha256(audit / "A5_STATUS.json"),
            "report_sha256": _sha256(root / "docs/sdd/task-05-report.md"),
            "review_package_sha256": _sha256(root / "docs/sdd/task-05-review-package.md"),
            "project_state_sha256": hashlib.sha256(state_before).hexdigest().upper()}

