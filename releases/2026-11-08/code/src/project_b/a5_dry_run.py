"""Synthetic-only Task05 orchestration and guarded authorization transition."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import gc
import math
import shutil
import threading
import csv
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


FORBIDDEN_ARTIFACT_KEYS = {
    "coefficient", "prediction", "performance", "shap", "model_object", "temporal_value",
}


def dimension_matched_benchmark_plan() -> dict[str, Any]:
    return {
        "n": 2272,
        "p_core": 20,
        "p_max": 177,
        "event_n": 348,
        "repetitions": 3,
        "future_survival_fit_counts": {"M0": 306, "EN": 3876, "RSF": 4182, "GBST": 6222},
        "planned_survival_fits": 14586,
        "development_evaluation_completed": 1440720,
        "development_evaluation_attempt_ceiling": 1584720,
        "max_fit_workers": 12,
        "future_wall_hours_limit": 96,
        "future_cpu_core_hours_limit": 800,
        "peak_process_tree_rss_bytes_limit": 32 * 1024 ** 3,
        "synthetic_wall_hours_limit": 8,
    }


def extrapolate_compute_budget(family_summaries: Mapping[str, Mapping[str, float]]) -> dict[str, Any]:
    plan = dimension_matched_benchmark_plan()
    fit_seconds = sum(
        plan["future_survival_fit_counts"][family] * float(family_summaries[family]["p95_seconds"])
        for family in ("M0", "EN", "RSF", "GBST")
    )
    evaluation_seconds_per_attempt = (
        float(family_summaries["CALIBRATION"]["p95_seconds"])
        + float(family_summaries["METRIC_REGRESSION_TRIPLET"]["p95_seconds"])
    )
    completed_evaluation_seconds = plan["development_evaluation_completed"] * evaluation_seconds_per_attempt
    worst_evaluation_seconds = plan["development_evaluation_attempt_ceiling"] * evaluation_seconds_per_attempt
    cpu_completed = (fit_seconds + completed_evaluation_seconds) / 3600.0
    cpu_upper = (fit_seconds + worst_evaluation_seconds) / 3600.0
    peak_worker = max(int(family_summaries[name]["peak_rss_bytes"])
                      for name in family_summaries)
    peak_tree = plan["max_fit_workers"] * peak_worker
    wall_upper = cpu_upper / plan["max_fit_workers"]
    status = "PASS" if (
        cpu_upper <= plan["future_cpu_core_hours_limit"]
        and wall_upper <= plan["future_wall_hours_limit"]
        and peak_tree <= plan["peak_process_tree_rss_bytes_limit"]
    ) else "COMPUTE_HOLD"
    return {
        "status": status,
        "future_survival_fit_seconds_upper": fit_seconds,
        "evaluation_seconds_per_attempt_upper": evaluation_seconds_per_attempt,
        "future_cpu_hours_completed_target": cpu_completed,
        "future_cpu_hours_upper": cpu_upper,
        "future_wall_hours_upper": wall_upper,
        "future_peak_process_tree_rss_bytes_upper": peak_tree,
        "assumption": f"family_p95_single_thread_and_{plan['max_fit_workers']}_nonoverlapping_workers",
    }


def _current_rss_bytes() -> int:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
            ]
        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        get_current_process = ctypes.windll.kernel32.GetCurrentProcess
        get_current_process.restype = wintypes.HANDLE
        process = get_current_process()
        get_memory = ctypes.windll.psapi.GetProcessMemoryInfo
        get_memory.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessMemoryCounters), wintypes.DWORD]
        get_memory.restype = wintypes.BOOL
        if not get_memory(process, ctypes.byref(counters), counters.cb):
            raise OSError("GetProcessMemoryInfo failed")
        return int(counters.WorkingSetSize)
    import resource
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if sys.platform == "darwin" else value * 1024)


def _timed_synthetic_call(callable_) -> tuple[float, int]:
    peak = [_current_rss_bytes()]
    stop = threading.Event()
    def sample():
        while not stop.wait(0.02):
            peak[0] = max(peak[0], _current_rss_bytes())
    sampler = threading.Thread(target=sample, name="a5-rss-sampler", daemon=True)
    sampler.start()
    started = time.perf_counter()
    try:
        callable_()
    finally:
        elapsed = time.perf_counter() - started
        peak[0] = max(peak[0], _current_rss_bytes())
        stop.set()
        sampler.join(timeout=2)
        gc.collect()
    return float(elapsed), int(peak[0])


def _percentile95(values) -> float:
    import numpy as np
    return float(np.percentile(np.asarray(values, dtype=float), 95, method="linear"))


def run_dimension_matched_benchmark(fixture_seed: int = 2026102405,
                                    project_root: str | Path | None = None) -> dict[str, Any]:
    """Run the brief-mandated full-dimension benchmark on generated data only."""
    import numpy as np
    from .a5_m0_cox import fit_m0
    from .a5_en_cox import fit_m2_en, _efron_loglik_gradient
    from .a5_rsf import fit_rsf
    from .a5_gbst import fit_gbst
    from .a5_common import derive_model_seed
    from .a5_calibration import fit_logistic_recalibrator, fit_evaluation_regression_triplet
    plan = dimension_matched_benchmark_plan()
    rng = np.random.default_rng(fixture_seed)
    x = rng.normal(size=(plan["n"], plan["p_max"])).astype(np.float64)
    x = (x - x.mean(axis=0)) / x.std(axis=0, ddof=0)
    event = np.zeros(plan["n"], dtype=int)
    event_indices = rng.choice(plan["n"], size=plan["event_n"], replace=False)
    event[event_indices] = 1
    times = np.full(plan["n"], 180.0)
    times[event_indices] = rng.integers(0, 180, size=plan["event_n"]).astype(float)
    records: list[dict[str, Any]] = []

    def measure(family: str, variant: str, repetition: int, call):
        seconds, rss = _timed_synthetic_call(call)
        records.append({"family": family, "variant": variant, "repetition": repetition,
                        "seconds": seconds, "peak_rss_bytes": rss, "status": "PASS"})

    started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    wall_started = time.perf_counter()
    for repetition in range(1, 4):
        def call_m0():
            model = fit_m0(x[:, :20], times, event, {"synthetic_fixture": True})
            if model["fit_call_count"] != 1:
                raise RuntimeError("M0 benchmark fit accounting drift")
        measure("M0", "p20_fixed", repetition, call_m0)

    # The smallest-ratio EN candidates are defined relative to the conditional
    # core-only score. The preparation fit is recorded but excluded from the
    # family p95 because it belongs to M0 in the formal plan.
    core_model = fit_m0(x[:, :20], times, event, {"synthetic_fixture": True})
    beta0 = np.zeros(plan["p_max"], dtype=float)
    beta0[:20] = core_model["backend"].params_.to_numpy(dtype=float)
    _, conditional_score = _efron_loglik_gradient(beta0, x, times, event)
    max_extension_score = float(np.max(np.abs(conditional_score[20:])))
    del core_model
    for alpha in (0.1, 0.5, 0.9):
        lambda_max = max_extension_score / (plan["event_n"] * alpha)
        scientific_lambda = lambda_max * 0.00390625
        for repetition in range(1, 4):
            def call_en(a=alpha, lam=scientific_lambda):
                model = fit_m2_en(x, times, event, [0] * 20 + [1] * 157,
                                  {"alpha": a, "lambda": lam},
                                  {"synthetic_fixture": True})
                if model["mapping"]["a"] != a:
                    raise RuntimeError("EN benchmark candidate drift")
            measure("EN", f"p177_alpha={alpha}_lambda_ratio=0.00390625", repetition, call_en)

    for width in (20, 177):
        candidate = {"mtry": math.ceil(width / 3), "sample_fraction": 0.80,
                     "min_samples_leaf": 32}
        for repetition in range(1, 4):
            def call_rsf(w=width, candidate_=candidate, rep=repetition):
                model = fit_rsf(x[:, :w], times, event, candidate_,
                                {"synthetic_fixture": True,
                                 "random_state": derive_model_seed(f"benchmark_p{w}", "RSF", "slow", str(rep), {"master_seed": fixture_seed})})
                if model["parameters"]["n_estimators"] != 1000:
                    raise RuntimeError("RSF benchmark estimator drift")
            measure("RSF", f"p{width}_slow_candidate", repetition, call_rsf)

    gbst_candidate = {"max_depth": 3, "learning_rate": 0.03, "n_estimators": 500,
                      "subsample": 0.80, "max_features": 1.0,
                      "min_samples_leaf": 64, "loss": "coxph",
                      "early_stopping": False}
    for width in (20, 177):
        for repetition in range(1, 4):
            def call_gbst(w=width, rep=repetition):
                model = fit_gbst(x[:, :w], times, event, gbst_candidate,
                                 {"synthetic_fixture": True,
                                  "random_state": derive_model_seed(f"benchmark_p{w}", "GBST", "slow", str(rep), {"master_seed": fixture_seed})})
                if model["parameters"]["n_estimators"] != 500:
                    raise RuntimeError("GBST benchmark estimator drift")
            measure("GBST", f"p{width}_slow_candidate", repetition, call_gbst)

    risk = 1.0 / (1.0 + np.exp(-np.clip(x[:, 0] * 0.7 - x[:, 1] * 0.2, -5, 5)))
    outcome = event.astype(int)
    for repetition in range(1, 4):
        def call_calibration():
            fit_logistic_recalibrator(risk, outcome, 180, {"source_role": "inner_oof"})
        measure("CALIBRATION", "one_logistic_recalibrator", repetition, call_calibration)
        def call_triplet():
            fit_evaluation_regression_triplet(risk, outcome, {"synthetic_fixture": True})
        measure("METRIC_REGRESSION_TRIPLET", "intercept_slope_curve", repetition, call_triplet)

    family_summaries: dict[str, dict[str, Any]] = {}
    for family in ("M0", "EN", "RSF", "GBST", "CALIBRATION", "METRIC_REGRESSION_TRIPLET"):
        rows = [row for row in records if row["family"] == family]
        family_summaries[family] = {
            "run_n": len(rows), "p95_seconds": _percentile95([row["seconds"] for row in rows]),
            "peak_rss_bytes": max(int(row["peak_rss_bytes"]) for row in rows),
            "all_pass": all(row["status"] == "PASS" for row in rows),
        }
    budget = extrapolate_compute_budget(family_summaries)
    wall_seconds = time.perf_counter() - wall_started
    if wall_seconds > plan["synthetic_wall_hours_limit"] * 3600:
        budget["status"] = "COMPUTE_HOLD"
    disk = None
    if project_root is not None:
        usage = shutil.disk_usage(Path(project_root).resolve())
        disk = {"free_bytes_after": usage.free, "minimum_free_bytes": 100 * 1024 ** 3,
                "pass": usage.free >= 100 * 1024 ** 3}
        if not disk["pass"]:
            budget["status"] = "COMPUTE_HOLD"
    return {
        "status": budget["status"], "synthetic_fixture": True,
        "real_patient_fit_n": 0, "database_connection_n": 0,
        "temporal_value_access_n": 0, "fixture_seed": fixture_seed,
        "started_at_utc": started_at,
        "completed_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "wall_seconds": wall_seconds, "plan": plan, "records": records,
        "family_summaries": family_summaries, "budget": budget, "disk": disk,
        "synthetic_survival_fit_n": 3 + 1 + 9 + 6 + 6,
        "synthetic_recalibration_fit_n": 3,
        "synthetic_evaluation_triplet_n": 3,
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Project_B Task05 synthetic dry-run")
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--synthetic-only", action="store_true", required=True)
    return parser


def prepare_pending_state(current: Mapping[str, Any], *, a5_status: str,
                          independent_review: str) -> dict[str, Any]:
    if a5_status != "PASS":
        raise PermissionError("A5 HOLD cannot create a pending transition")
    if current.get("phase") != "A5" or current.get("fit_authorized") is not False or \
            current.get("temporal_unsealed") is not False:
        raise PermissionError("unexpected A5 pre-state")
    proposed = dict(current)
    proposed.update({"phase": "A5", "project_status": "IN_PROGRESS",
                     "fit_authorized": False, "temporal_unsealed": False})
    return {"status": "PENDING_INDEPENDENT_REVIEW_AND_SIGNATURE",
            "independent_review": independent_review, "proposed_state": proposed,
            "state_modified": False}


def validate_fit_authorization(*, a5_status: Mapping[str, Any], review: Mapping[str, Any] | None,
                               draft: Mapping[str, Any] | None,
                               signature: Mapping[str, Any] | None) -> dict[str, Any]:
    if a5_status.get("conclusion") != "PENDING_INDEPENDENT_REVIEW":
        raise PermissionError("A5 has not passed")
    if (
        a5_status.get("independent_review") != "PENDING" or a5_status.get("hold_reasons") != [] or
        a5_status.get("implementation_gates_status") != "PASS" or
        a5_status.get("gates") != {name: "PASS" for name in ("authority", "software", "numerical", "bootstrap", "compute")}
    ):
        raise PermissionError("A5 implementation snapshot is not eligible for review")
    if not review or review.get("spec_compliance") != "PASS" or review.get("code_quality") != "APPROVED":
        raise PermissionError("independent review missing")
    if not draft or not signature:
        raise PermissionError("immutable draft and manual signature are required")
    exact = {"schema_version", "researcher_id", "approved_at_utc", "authorization_draft_path",
             "authorization_draft_sha256", "approved"}
    if set(signature) != exact or signature.get("schema_version") != "1.0" or \
            not isinstance(signature.get("researcher_id"), str) or not signature["researcher_id"].strip() or signature.get("approved") is not True or \
            signature.get("authorization_draft_path") != "audit/ML_FIT_AUTHORIZATION.json" or \
            not re.fullmatch(r"[0-9A-F]{64}", str(signature.get("authorization_draft_sha256", ""))) or \
            not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", str(signature.get("approved_at_utc", ""))):
        raise PermissionError("manual signature schema invalid")
    try:
        datetime.fromisoformat(signature["approved_at_utc"][:-1] + "+00:00")
    except (ValueError, TypeError) as exc:
        raise PermissionError("manual signature UTC timestamp invalid") from exc
    return {"status": "PASS"}


def _validate_authorization_bundle(root: Path, draft_path: Path, supplied_signature):
    """Recheck the complete immutable bundle, including the post-snapshot review."""
    from .a5_common import AUTHORITY_HASHES, validate_researcher_decisions
    expected_draft = root / "audit/ML_FIT_AUTHORIZATION.json"
    signature_path = root / "audit/a5/ML_FIT_RESEARCHER_SIGNATURE.json"
    if draft_path.resolve() != expected_draft.resolve():
        raise PermissionError("authorization draft must use the fixed path")
    for relative, digest in AUTHORITY_HASHES.items():
        path = root / relative
        if not path.is_file() or _sha256(path) != digest:
            raise PermissionError("authorization authority hash drift")
    decisions = json.loads((root / "audit/a5/A5_RESEARCHER_DECISIONS.json").read_text(encoding="utf-8"))
    validate_researcher_decisions(decisions)
    a4_review = json.loads((root / "audit/a4/A4_INDEPENDENT_REVIEW.json").read_text(encoding="utf-8"))
    if a4_review.get("spec_compliance") != "PASS" or a4_review.get("code_quality") != "APPROVED":
        raise PermissionError("A4 review is not approved")
    with (root / "manifests/A4_FREEZE_MANIFEST.csv").open(encoding="utf-8", newline="") as handle:
        if sum(1 for _ in csv.DictReader(handle)) != 63:
            raise PermissionError("A4 manifest count drift")
    status_path = root / "audit/a5/A5_STATUS.json"
    review_path = root / "audit/a5/A5_INDEPENDENT_REVIEW.json"
    manifest_path = root / "audit/a5/A5_HASH_MANIFEST.csv"
    expected_path = root / "config/A5_EXPECTED_ARTIFACTS.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    pending = json.loads((root / "audit/a5/A5_STATE_TRANSITION_PENDING.json").read_text(encoding="utf-8"))
    review = json.loads(review_path.read_text(encoding="utf-8"))
    draft = json.loads(expected_draft.read_text(encoding="utf-8"))
    signature = json.loads(signature_path.read_text(encoding="utf-8"))
    validate_fit_authorization(a5_status=status, review=review, draft=draft, signature=signature)
    if supplied_signature is not None and dict(supplied_signature) != signature:
        raise PermissionError("caller signature differs from the manual signature file")
    if review.get("task") != "Task05" or review.get("review_scope") != "FULL_TASK05" or review.get("critical_findings") != 0 or review.get("important_findings") != 0:
        raise PermissionError("independent review has open findings")
    if status.get("phase") != "A5" or status.get("project_status") != "IN_PROGRESS" or status.get("fit_authorized") is not False or status.get("temporal_unsealed") is not False or status.get("state_modified") is not False or status.get("test_n", 0) < 1 or status.get("test_pass_n") != status.get("test_n") or status.get("test_fail_n") != 0 or status.get("test_error_n") != 0:
        raise PermissionError("snapshot state or test results are not eligible")
    pre_state = pending.get("proposed_state")
    if pending.get("status") != "PENDING_INDEPENDENT_REVIEW_AND_SIGNATURE" or pending.get("state_modified") is not False or pending.get("current_state_sha256") != status.get("state_sha256_before") or not isinstance(pre_state, dict) or pre_state.get("phase") != "A5" or pre_state.get("project_status") != "IN_PROGRESS" or pre_state.get("fit_authorized") is not False or pre_state.get("temporal_unsealed") is not False or not pre_state.get("initialized_at") or not pre_state.get("proposal_sha256"):
        raise PermissionError("pending transition does not bind the exact pre-state")
    def utc(value):
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", value):
            raise PermissionError("invalid UTC evidence timestamp")
        try:
            return datetime.fromisoformat(value[:-1] + "+00:00")
        except ValueError as exc:
            raise PermissionError("invalid UTC evidence timestamp") from exc
    if not utc(status.get("captured_at_utc")) <= utc(review.get("reviewed_at_utc")) <= utc(signature["approved_at_utc"]):
        raise PermissionError("snapshot, review and signature chronology is invalid")
    expected = json.loads(expected_path.read_text(encoding="utf-8"))
    paths = expected.get("paths", [])
    from .a5_finalize import validate_expected_paths
    validate_expected_paths(paths)
    with manifest_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) != expected.get("artifact_n") or [row.get("relative_path") for row in rows] != paths:
        raise PermissionError("A5 manifest membership or order drift")
    for row in rows:
        path = (root / row["relative_path"]).resolve()
        if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size != int(row["bytes"]) or _sha256(path) != row["sha256"] or row["status"] != "FROZEN":
            raise PermissionError("A5 manifest content drift")
    mandatory = {"FINAL_PROPOSAL.md", "manifests/A4_FREEZE_MANIFEST.csv", "config/SOFTWARE_LOCK.json",
                 "audit/a5/A5_RESEARCHER_DECISIONS.json", "audit/a5/A5_TEST_RESULTS.txt", "audit/a5/A5_STATUS.json", "audit/a5/A5_STATE_TRANSITION_PENDING.json",
                 "audit/a5/A5_HASH_MANIFEST.csv", "audit/a5/A5_INDEPENDENT_REVIEW.json"}
    mandatory.update(path for path in paths if path.startswith("config/"))
    bindings = draft.get("bindings", {})
    if set(bindings) != mandatory or any(not (root / path).is_file() or _sha256(root / path) != bindings[path] for path in mandatory):
        raise PermissionError("authorization draft bindings mismatch")
    for key, path in (("manifest", "audit/a5/A5_HASH_MANIFEST.csv"),
                      ("status_snapshot", "audit/a5/A5_STATUS.json"),
                      ("test_results", "audit/a5/A5_TEST_RESULTS.txt")):
        entry = review.get(key, {})
        if entry.get("path") != path or entry.get("sha256") != _sha256(root / path):
            raise PermissionError("independent review binding mismatch")
    tests = review["test_results"]
    if tests.get("exit_code") != 0 or tests.get("failed_n") != 0 or tests.get("test_n", 0) < 1 or tests.get("passed_n") != tests.get("test_n") or tests.get("test_n") != status["test_n"]:
        raise PermissionError("independent test receipt did not pass")
    required_scope = {"development_years": [2008, 2019], "nested_development_and_final_fit_only": True,
                      "temporal": False, "compression": False, "dca": False, "tool_construction": False,
                      "alternate_algorithms": False, "definition_changes": False}
    limits = {"max_fit_workers": 12, "estimator_threads": 1, "peak_process_tree_rss_gib": 32, "artifact_and_temporary_gib": 50,
              "minimum_free_gib": 100, "synthetic_wall_hours": 8, "development_wall_hours": 96, "development_cpu_core_hours": 800,
              "temporal_wall_hours_provisional": 24, "temporal_cpu_core_hours_provisional": 96,
              "temporal_peak_rss_gib_provisional": 32, "temporal_disk_gib_provisional": 20}
    if draft.get("schema_version") != "1.0" or draft.get("scope") != required_scope or draft.get("resource_limits") != limits or draft.get("trainable_object_ceiling") != 15198:
        raise PermissionError("authorization scope or limits drift")
    digest = _sha256(expected_draft)
    if signature["authorization_draft_sha256"] != digest:
        raise PermissionError("signature does not bind byte-identical draft")
    return status, {"authorization_draft_sha256": digest, "signature_sha256": _sha256(signature_path)}, pre_state


@contextmanager
def _transition_lock(path: Path):
    # A process-held advisory lock is released even if the process crashes.
    handle = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            if path.stat().st_size == 0:
                handle.write(b"\0"); handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        handle.close()


def _atomic_bytes(path: Path, content: bytes):
    descriptor, name = tempfile.mkstemp(prefix=".a5-transition-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content); handle.flush(); os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def apply_signed_transition(project_root: str | Path, draft_path: str | Path | None = None,
                            signature: Mapping[str, Any] | None = None) -> dict[str, Any]:
    from .a5_common import _project_root
    root = _project_root(project_root)
    draft_path = Path(draft_path) if draft_path is not None else root / "audit/ML_FIT_AUTHORIZATION.json"
    _validate_authorization_bundle(root, draft_path, signature)
    state_path = root / "manifests/PROJECT_STATE.json"
    receipt_path = root / "audit/a5/ML_FIT_STATE_TRANSITION.json"
    with _transition_lock(root / "audit/a5/.ml-fit-transition.lock"):
        status, identity, pre_state = _validate_authorization_bundle(root, draft_path, signature)
        current_bytes = state_path.read_bytes()
        current_hash = hashlib.sha256(current_bytes).hexdigest().upper()
        new_state = dict(pre_state)
        new_state.update({"phase": "MODEL_DEVELOPMENT", "project_status": "IN_PROGRESS",
                          "fit_authorized": True, "temporal_unsealed": False, "compression_branch": "PENDING"})
        target = (json.dumps(new_state, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        target_hash = hashlib.sha256(target).hexdigest().upper()
        if receipt_path.is_file():
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            if any(receipt.get(key) != value for key, value in identity.items()) or receipt.get("state_sha256") != target_hash or receipt.get("previous_state_sha256") != status.get("state_sha256_before"):
                raise PermissionError("DUPLICATE_TRANSITION_REJECTED")
            if current_hash == receipt.get("state_sha256"):
                if receipt.get("status") == "APPLIED":
                    return {"status": "ALREADY_APPLIED", **identity}
                if receipt.get("status") == "COMMIT_INTENT":
                    receipt["status"] = "APPLIED"
                    _atomic_bytes(receipt_path, (json.dumps(receipt, indent=2) + "\n").encode("utf-8"))
                    return receipt
                raise PermissionError("DUPLICATE_TRANSITION_REJECTED")
            if current_hash != receipt.get("previous_state_sha256"):
                raise PermissionError("DUPLICATE_TRANSITION_REJECTED")
            if receipt.get("status") != "COMMIT_INTENT":
                raise PermissionError("DUPLICATE_TRANSITION_REJECTED")
        else:
            receipt = None
        state = json.loads(current_bytes)
        if state.get("phase") != "A5" or state.get("fit_authorized") is not False or state.get("temporal_unsealed") is not False or state.get("project_status") != "IN_PROGRESS":
            raise PermissionError("unexpected pre-transition state")
        if current_hash != status.get("state_sha256_before"):
            raise PermissionError("snapshot does not bind the exact pre-state")
        if state != pre_state:
            raise PermissionError("pending transition state payload mismatch")
        if receipt is None:
            receipt = {"status": "COMMIT_INTENT", **identity, "previous_state_sha256": current_hash,
                       "state_sha256": target_hash,
                       "transition_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
            # Commit the durable receipt first: interruption leaves the old,
            # unauthorized state and is safely completed by identical replay.
            _atomic_bytes(receipt_path, (json.dumps(receipt, indent=2) + "\n").encode("utf-8"))
        elif receipt.get("state_sha256") != target_hash:
            raise PermissionError("DUPLICATE_TRANSITION_REJECTED")
        _atomic_bytes(state_path, target)
        receipt["status"] = "APPLIED"
        _atomic_bytes(receipt_path, (json.dumps(receipt, indent=2) + "\n").encode("utf-8"))
        return receipt


def validate_a5_artifact_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    def walk(value, path=""):
        if isinstance(value, Mapping):
            for key, child in value.items():
                lowered = str(key).lower()
                if lowered in FORBIDDEN_ARTIFACT_KEYS:
                    raise ValueError(f"forbidden A5 artifact key: {path}{key}")
                walk(child, f"{path}{key}.")
        elif isinstance(value, (list, tuple)):
            for child in value:
                walk(child, path)
    walk(payload)
    return {"status": "PASS"}


def fit_recalibration_stream(risks, outcomes, horizon):
    from .a5_calibration import fit_logistic_recalibrator
    try:
        model = fit_logistic_recalibrator(risks, outcomes, horizon, {"source_role": "inner_oof"})
    except (ValueError, RuntimeError, FloatingPointError) as exc:
        return {"status": "RECALIBRATION_STREAM_HOLD", "reason": str(exc)}
    return {"status": "PASS", "model": model}


def run_synthetic_dry_run(config_root: str | Path, fixture_seed: int = 2026102405) -> dict[str, Any]:
    from .a5_common import validate_a5_authorities, validate_installed_software_lock
    root = Path(config_root).resolve()
    authority = validate_a5_authorities(root)
    software = validate_installed_software_lock(root)
    try:
        result = _run_synthetic_integration(root, fixture_seed)
    except ValueError as exc:
        # Only recognized scientific contract failures are HOLDs. Unexpected
        # exceptions remain execution errors and must not be reported as a gate.
        scientific_codes = (
            "CORE_DATA_FAILURE", "CORE_DEGENERACY", "BINARY_CONTRACT_FAILURE",
            "M0_NUMERICAL_HOLD", "NATIVE_MONOTONICITY_FAILURE",
            "INVALID_NATIVE_PROBABILITY", "PATIENT_FOLD_COLUMN_CHANGE",
            "OUTCOME_TRUNCATION_INCONSISTENCY", "LEAKAGE", "MISSING_PROVENANCE",
            "OBJECTIVE_TIES_BASELINE_DRIFT", "SOFTWARE_VERSION_DRIFT",
        )
        reason = next((code for code in scientific_codes if code in str(exc)), None)
        if reason is None:
            raise
        result = {"status": "HOLD", "hold_reason": reason, "details": str(exc)}
    return {**result, "synthetic_fixture": True, "fixture_seed": fixture_seed,
            "authority_status": authority["status"], "software_status": software["status"],
            "real_patient_fit_n": 0, "database_connection_n": 0, "temporal_value_access_n": 0}


def _run_synthetic_integration(root: Path, fixture_seed: int) -> dict[str, Any]:
    """Run real synthetic five-fold fits. A single bootstrap attempt cannot PASS D4."""
    import numpy as np
    from .a5_bootstrap import attempt_indices, compute_synthetic_attempt
    from .a5_calibration import apply_recalibrator
    from .a5_common import generate_inner_folds, derive_model_seed
    from .a5_en_cox import en_grid, fit_m2_en
    from .a5_gbst import gbst_grid, fit_gbst
    from .a5_m0_cox import fit_m0, predict_native_risk
    from .a5_preprocessing import C2_COLUMNS, load_frozen_preprocessing_spec, fit_preprocessor, transform_preprocessor
    from .a5_rsf import rsf_grid, fit_rsf
    from .a5_selection import conditional_en_lambdas, evaluate_candidate_grid, candidate_for_fold

    rng = np.random.default_rng(fixture_seed)
    n = 400
    core = rng.normal(size=(n, 20))
    ext = rng.normal(size=(n, 2))
    latent = rng.exponential(120 / np.exp(np.clip(core[:, 0] * 0.25 + ext[:, 0] * 0.3, -2, 2)))
    event = (latent <= 180).astype(int)
    time_values = np.where(event == 1, np.floor(latent / 3) * 3, 180.0)
    metadata = load_frozen_preprocessing_spec(root)
    metadata["partition_role"] = "inner_training"
    numeric = [name for name in metadata["field_order"] if name not in metadata["binary_columns"]][:2]
    core_rows = [dict(zip(C2_COLUMNS, row)) for row in core]
    default = {name: (0 if name in metadata["binary_columns"] else None) for name in metadata["field_order"]}
    extension_rows = []
    for values in ext:
        row = dict(default); row[numeric[0]] = float(values[0]); row[numeric[1]] = float(values[1])
        extension_rows.append(row)
    identities = [(index, index) for index in range(n)]
    assignment = np.asarray(generate_inner_folds(identities, event, f"A5|{fixture_seed}|full_development",
                                                   {"master_seed": fixture_seed, "inner_folds": 5}))
    folds, data = [], []
    m0_oof = np.empty((n, 2))
    for k in range(1, 6):
        train = np.flatnonzero(assignment != k).tolist()
        valid = np.flatnonzero(assignment == k).tolist()
        en_pre = fit_preprocessor(core_rows, extension_rows, train, metadata, "EN")
        tree_pre = fit_preprocessor(core_rows, extension_rows, train, metadata, "RSF")
        m1_pre = fit_preprocessor(core_rows, None, train, {**metadata, "model_level": "M1"}, "RSF")
        if en_pre["extension_columns"] != tree_pre["extension_columns"]:
            raise ValueError("active extension set differs across model families")
        en_train = np.asarray(transform_preprocessor(en_pre, core_rows, extension_rows, train))
        en_valid = np.asarray(transform_preprocessor(en_pre, core_rows, extension_rows, valid))
        tree_train = np.asarray(transform_preprocessor(tree_pre, core_rows, extension_rows, train))
        tree_valid = np.asarray(transform_preprocessor(tree_pre, core_rows, extension_rows, valid))
        m1_train = np.asarray(transform_preprocessor(m1_pre, core_rows, None, train))
        m1_valid = np.asarray(transform_preprocessor(m1_pre, core_rows, None, valid))
        m0 = fit_m0(en_train[:, :20], time_values[train], event[train], {"synthetic_fixture": True})
        m0_oof[valid] = predict_native_risk(m0, en_valid[:, :20])
        lambdas = conditional_en_lambdas(en_train, time_values[train], event[train], m0["backend"].params_.to_numpy())
        folds.append({"ids": valid, "outcomes": event[valid].tolist()})
        data.append({"train": train, "valid": valid, "en_train": en_train, "en_valid": en_valid,
                     "tree_train": tree_train, "tree_valid": tree_valid,
                     "m1_train": m1_train, "m1_valid": m1_valid, "lambdas": lambdas})
    width = data[0]["tree_train"].shape[1]
    models = (("M2_EN", "EN", False), ("M1_RSF", "RSF", True), ("M2_RSF", "RSF", False),
              ("M1_GBST", "GBST", True), ("M2_GBST", "GBST", False))
    caches = {name: {} for name, _, _ in models}
    results = {}
    for name, family, core_only in models:
        feature_width = 20 if core_only else width
        grid = en_grid() if family == "EN" else rsf_grid(feature_width) if family == "RSF" else gbst_grid()
        def fit_fold(candidate, fold_no, fold, family=family, name=name, core_only=core_only):
            item = data[fold_no - 1]; train, valid = item["train"], item["valid"]
            seed = derive_model_seed("synthetic_dry_run", name, str(candidate["declared_order"]),
                                     str(fold_no), {"master_seed": fixture_seed})
            if family == "EN":
                lam = item["lambdas"][candidate["declared_order"]]["lambda"]
                matrix = item["en_train"]
                model = fit_m2_en(matrix, time_values[train], event[train], [0]*20 + [1]*(matrix.shape[1]-20),
                                  {"alpha": candidate["alpha"], "lambda": lam}, {"synthetic_fixture": True})
                target = item["en_valid"]
                count = int(np.sum(np.abs(model["backend"].params_.to_numpy()[20:]) > 1e-8))
            elif family == "RSF":
                train_matrix = item["m1_train"] if core_only else item["tree_train"]
                target = item["m1_valid"] if core_only else item["tree_valid"]
                adapted_candidate = candidate_for_fold("RSF", candidate, n_features=train_matrix.shape[1])
                model = fit_rsf(train_matrix, time_values[train], event[train], adapted_candidate,
                                {"synthetic_fixture": True, "random_state": seed})
                count = 0
            else:
                train_matrix = item["m1_train"] if core_only else item["tree_train"]
                target = item["m1_valid"] if core_only else item["tree_valid"]
                model = fit_gbst(train_matrix, time_values[train], event[train], candidate,
                                 {"synthetic_fixture": True, "random_state": seed})
                count = 0
            risks = np.asarray(predict_native_risk(model, target))
            caches[name][(candidate["declared_order"], fold_no)] = risks
            return {"ids": valid, "risk180": risks[:, 1].tolist(), "nonzero_count": count}
        results[name] = evaluate_candidate_grid(family, grid, folds, fit_fold,
                                                n_features=feature_width if family == "RSF" else None)
        if results[name]["status"] != "PASS":
            return {"status": "HOLD", "hold_reason": results[name].get("project_failure", f"{name}_CANDIDATE_HOLD"),
                    "candidate_ledgers": {key: row["ledger"] for key, row in results.items()}}
    native = {"M0": m0_oof}
    for name, _, _ in models:
        output = np.empty((n, 2)); selected = results[name]["selected"]["declared_order"]
        for fold_no, item in enumerate(data, 1):
            output[item["valid"]] = caches[name][(selected, fold_no)]
        native[name] = output
    outcomes = {"time": time_values, "event": event, "death_90d": (event == 1) & (time_values <= 90),
                "death_180d": event}
    streams = {}
    for name, risks in native.items():
        recal = np.empty_like(risks)
        for h, horizon in enumerate((90, 180)):
            fit = fit_recalibration_stream(risks[:, h], outcomes[f"death_{horizon}d"], horizon)
            if fit["status"] != "PASS":
                return {"status": "HOLD", "hold_reason": "RECALIBRATION_STREAM_HOLD",
                        "held_stream": f"{name}|{horizon}", "reason": fit["reason"],
                        "native_streams_unchanged": True,
                        "candidate_ledgers": {key: row["ledger"] for key, row in results.items()}}
            recal[:, h] = apply_recalibrator(fit["model"], risks[:, h])
        streams[f"{name}_native"] = np.repeat(risks[None, :, :], 10, axis=0)
        streams[f"{name}_recal"] = np.repeat(recal[None, :, :], 10, axis=0)
    draw = attempt_indices(n, attempt_id=1, spec={"master_seed": fixture_seed, "common_validity": "GLOBAL_COMMON_VALID"})
    try:
        output = compute_synthetic_attempt(streams, outcomes, draw)
        attempt_status, reason = "VALID", None
    except (ValueError, RuntimeError, FloatingPointError) as exc:
        output = {}; attempt_status, reason = "REJECTED", str(exc)
    return {"status": "HOLD", "hold_reason": "BOOTSTRAP_INTERVALS_HOLD",
            "gates": {name: "PASS" for name in ("preprocessing", "m0", "en", "rsf", "gbst", "calibration")},
            "candidate_ledgers": {key: row["ledger"] for key, row in results.items()},
            "synthetic_survival_fits": {"planned": 5 + sum(row["ledger"]["planned"] for row in results.values()),
                                        "attempted": 5 + sum(row["ledger"]["attempted"] for row in results.values()),
                                        "succeeded": 5 + sum(row["ledger"]["succeeded"] for row in results.values()),
                                        "failed": sum(row["ledger"]["failed"] for row in results.values()),
                                        "skipped": sum(row["ledger"]["skipped"] for row in results.values())},
            "synthetic_recalibration_fits": 12,
            "bootstrap": {"status": "BOOTSTRAP_INTERVALS_HOLD", "actual_attempts": 1,
                          "attempt_status": attempt_status, "rejection_reason": reason,
                          "required_output_n": len(output), "intervals": [],
                          "fixture_repeats": "ten_copies_of_one_synthetic_oof_pass_not_ten_independent_repeats"}}


def synthetic_backend_conformance(fixture_seed: int = 2026102405) -> dict[str, Any]:
    """Exercise the frozen tree backends only on generated data."""
    import numpy as np
    import sksurv
    from .a5_rsf import fit_rsf, rsf_grid
    from .a5_gbst import fit_gbst, gbst_grid
    from .a5_common import derive_model_seed
    from .a5_m0_cox import predict_native_risk
    rng = np.random.default_rng(fixture_seed)
    n, p = 360, 12
    matrix = rng.normal(size=(n, p))
    latent = rng.exponential(60 / np.exp(np.clip(matrix[:, 0] * 0.4, -1.5, 1.5)))
    censor = rng.uniform(50, 210, size=n)
    event = (latent <= censor).astype(int)
    time = np.minimum(np.minimum(latent, censor), 180.0)
    event = ((event == 1) & (time <= 180)).astype(int)
    time[event == 0] = 180.0
    time[int(np.flatnonzero(event)[0])] = 90.0
    checks = []
    for leaf in (32, 96):
        candidate = next(row for row in rsf_grid(p) if row["min_samples_leaf"] == leaf and row["sample_fraction"] == 0.8)
        seed = derive_model_seed("conformance", "RSF", str(candidate["declared_order"]), "final", {"master_seed": fixture_seed})
        model = fit_rsf(matrix, time, event, candidate, {"synthetic_fixture": True, "random_state": seed})
        estimator = model["backend"]
        leaf_minimum = min(int(tree.tree_.n_node_samples[index]) for tree in estimator.estimators_
                           for index in range(tree.tree_.node_count) if tree.tree_.children_left[index] == -1)
        target = matrix[:5]
        individual = np.mean([tree.predict_cumulative_hazard_function(target, return_array=True)
                              for tree in estimator.estimators_], axis=0)
        forest = estimator.predict_cumulative_hazard_function(target, return_array=True)
        idx = np.searchsorted(estimator.unique_times_, [89.0, 90.0], side="right") - 1
        expected = 1 - np.exp(-individual[:, idx])
        public = np.asarray(predict_native_risk(model, target, horizons=(89, 90)))
        checks.append({"min_samples_leaf": leaf, "observed_terminal_leaf_minimum": leaf_minimum,
                       "tree_forest_chf_max_abs_diff": float(np.max(np.abs(individual - forest))),
                       "public_risk_max_abs_diff": float(np.max(np.abs(public - expected)))})
    identity = all(row["observed_terminal_leaf_minimum"] >= row["min_samples_leaf"] and
                   row["tree_forest_chf_max_abs_diff"] <= 1e-12 and row["public_risk_max_abs_diff"] <= 1e-12
                   for row in checks)
    rsf_estimator = estimator
    gbst_candidate = gbst_grid()[0]
    seed = derive_model_seed("conformance", "GBST", str(gbst_candidate["declared_order"]), "final", {"master_seed": fixture_seed})
    gbst_model = fit_gbst(matrix, time, event, gbst_candidate,
                          {"synthetic_fixture": True, "random_state": seed})
    gbst_estimator = gbst_model["backend"]
    score = gbst_estimator.predict(matrix)
    target_score = gbst_estimator.predict(matrix[:5])
    increments = [(t, float(np.sum((time == t) & (event == 1)) / np.sum(np.exp(score[time >= t]))))
                  for t in np.unique(time[event == 1])]
    reference = np.column_stack([1 - np.exp(-sum(inc for t, inc in increments if t <= horizon) * np.exp(target_score))
                                 for horizon in (89, 90)])
    gbst_diff = float(np.max(np.abs(np.asarray(predict_native_risk(gbst_model, matrix[:5], horizons=(89, 90))) - reference)))
    return {"status": "PASS" if identity and gbst_diff <= 1e-12 and sksurv.__version__ == "0.28.0" else "HOLD",
            "rsf": {"class": type(rsf_estimator).__name__, "version": sksurv.__version__,
                    "min_samples_leaf": 32,
                    "observed_terminal_leaf_minimum": checks[0]["observed_terminal_leaf_minimum"],
                    "nelson_aalen_risk_identity": identity, "checks": checks},
            "gbst": {"class": type(gbst_estimator).__name__, "version": sksurv.__version__,
                     "loss": gbst_estimator.loss, "min_samples_leaf": gbst_estimator.min_samples_leaf,
                     "early_stopping": False, "risk_reconstruction": "training_only_breslow",
                     "public_breslow_max_abs_diff": gbst_diff}}


def synthetic_m0_identity_audit(fixture_seed: int = 2026102405) -> dict[str, Any]:
    import numpy as np
    import pandas as pd
    from scipy.optimize import minimize
    from .a5_preprocessing import generate_c2_design
    from .a5_m0_cox import fit_m0, alias_m1_en, predict_native_risk
    from .a5_en_cox import _efron_loglik_gradient, _breslow_risk
    rng = np.random.default_rng(fixture_seed)
    rows = []
    for index in range(260):
        rows.append({
            "age": int(rng.integers(18, 90)), "gender": "F" if rng.random() < 0.43 else "M",
            "charlson_prior_no_age": int(rng.integers(0, 7)),
            "rbc_completed_ml": float(rng.gamma(1.6, 180.0)),
            "non_rbc_component_ever": int(rng.random() < 0.28),
            "vasopressor_active_hours": float(rng.gamma(1.3, 5.0)),
            "cumulative_invasive_ventilation_hours": float(rng.gamma(2.2, 20.0)),
            "icp_monitor_or_csf_diversion_ever": int(rng.random() < 0.24),
            "cranial_operation_ever": int(rng.random() < 0.31),
            "icu_episode_hours": float(12.0 + rng.gamma(2.1, 30.0)),
            "modified_non_neuro_sofa2_total": int(rng.integers(0, 10)),
            "abnormal_non_neuro_organ_count": int(rng.integers(0, 6)),
            "nonreactive_pupil_count": int(rng.integers(0, 3)),
            "pupil_unassessable": int(rng.random() < 0.08),
            "gcsm": int(rng.integers(1, 7)),
            "wpr": None if index % 11 == 0 else 0.11 + (index % 80) / 1000,
            "wpr_unavailable": int(index % 11 == 0),
        })
    matrix, columns, learned = generate_c2_design(rows, list(range(200)))
    x = np.asarray(matrix, dtype=float)
    sd = x.std(axis=0, ddof=0)
    if np.any(sd == 0):
        raise RuntimeError("synthetic C2 fixture contains a degenerate core column")
    x = (x - x.mean(axis=0)) / sd
    truth = np.zeros(20); truth[:5] = [0.2, -0.12, 0.18, 0.08, -0.1]
    latent = rng.exponential(60 / np.exp(np.clip(x @ truth, -2, 2)))
    censor = rng.uniform(45, 220, size=len(x))
    event = (latent <= censor).astype(int)
    time = np.minimum(latent, censor)
    time = np.minimum(np.floor(time / 6) * 6, 180.0)
    event = ((event == 1) & (time <= 180)).astype(int)
    time[event == 0] = 180.0
    time[int(np.flatnonzero(event)[0])] = 0.0
    model = fit_m0(x, time, event, {"synthetic_fixture": True})
    alias = alias_m1_en(model)
    beta_backend = model["backend"].params_.to_numpy(dtype=float)
    event_n = int(event.sum())
    def objective_gradient(beta):
        loglik, score = _efron_loglik_gradient(beta, x, time, event)
        return -loglik / event_n, -score / event_n
    reference = minimize(objective_gradient, beta_backend, jac=True, method="BFGS",
                         options={"gtol": 1e-11, "maxiter": 3000})
    if not reference.success and float(np.max(np.abs(reference.jac))) > 1e-8:
        raise RuntimeError(f"M0 independent reference failed: {reference.message}")
    beta_reference = reference.x
    loglik_backend_manual, _ = _efron_loglik_gradient(beta_backend, x, time, event)
    target = x[:15]
    manual_risk = _breslow_risk(beta_reference, x, time, event, target)
    fitter = model["backend"]
    backend_risk = np.asarray(predict_native_risk(model, target))
    alias_risk = np.asarray(predict_native_risk(alias, target))
    alias_diff = float(np.max(np.abs(backend_risk - alias_risk)))
    frame = pd.DataFrame(target, columns=model["columns"])
    alias_backend = alias["object"]["backend"]
    linear_diff = float(np.max(np.abs(fitter.predict_log_partial_hazard(frame).to_numpy() -
                                       alias_backend.predict_log_partial_hazard(frame).to_numpy())))
    baseline_diff = float(np.max(np.abs(fitter.baseline_cumulative_hazard_.to_numpy() -
                                         alias_backend.baseline_cumulative_hazard_.to_numpy())))
    coefficient_diff = float(np.max(np.abs(beta_backend - beta_reference)))
    risk_diff = float(np.max(np.abs(backend_risk - manual_risk)))
    loglik_diff = abs(float(fitter.log_likelihood_) - loglik_backend_manual)
    status = "PASS" if coefficient_diff <= 1e-6 and risk_diff <= 1e-6 and loglik_diff <= 1e-8 and max(alias_diff, linear_diff, baseline_diff) <= 1e-12 else "HOLD"
    return {"status": status, "design_column_n": len(columns),
            "design_column_sha256": hashlib.sha256("\n".join(columns).encode()).hexdigest().upper(),
            "coefficient_max_abs_diff": coefficient_diff, "risk_max_abs_diff": risk_diff,
            "log_partial_likelihood_abs_diff": loglik_diff,
            "m1_en_additional_fit_call_count": alias["additional_fit_call_count"],
            "m1_en_identity_max_abs_diff": alias_diff,
            "m1_en_linear_predictor_max_abs_diff": linear_diff,
            "m1_en_baseline_chf_max_abs_diff": baseline_diff,
            "contains_zero_day_event": bool(np.any((time == 0) & (event == 1))),
            "contains_tied_events": len(set(time[event == 1].tolist())) < event_n,
            "contains_wpr_missingness": any(row["wpr"] is None for row in rows),
            "age_knots": learned["age_knots"], "wpr_median": learned["wpr_median"]}


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.synthetic_only:
        parser.error("--synthetic-only is mandatory")
    try:
        result = run_synthetic_dry_run(args.project_root)
        print(json.dumps(result, indent=2))
        return 0 if result.get("status") == "PASS" else 2
    except (PermissionError, ValueError, FileNotFoundError) as exc:
        print(f"Task05 HOLD: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"Task05 execution error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

