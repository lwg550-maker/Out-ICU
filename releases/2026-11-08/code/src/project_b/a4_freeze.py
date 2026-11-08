"""Task 04 A4 resampling and freeze controls.

The functions in this module are deliberately deterministic and side-effect
free, except for :func:`freeze_a4_bundle`, whose writes are restricted to the
canonical Project_B root supplied by the caller.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping


IDENTITY_FIELDS = ("subject_id", "hadm_id")
ALLOWED_ROLES = {"identity", "outcome", "core_raw", "extension_numeric", "extension_binary",
                 "extension_unavailable", "extension_freshness", "audit_only"}
FUTURE_TABLE_N = 3
FUTURE_AUDIT_N = 5


def _integer(value: Any, name: str) -> int:
    text = str(value).strip()
    try:
        number = int(text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a decimal integer") from exc
    if str(number) != text.lstrip("+"):
        raise ValueError(f"{name} must use canonical decimal notation")
    return number


def _canonical_identity(row: Mapping[str, Any]) -> tuple[int, int]:
    if set(row) < set(IDENTITY_FIELDS):
        raise ValueError("identity row is incomplete")
    return _integer(row["subject_id"], "subject_id"), _integer(row["hadm_id"], "hadm_id")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def generate_outer_folds(index_outcome: Iterable[Mapping[str, Any]],
                         resampling_spec: Mapping[str, Any]) -> list[dict[str, str]]:
    """Generate the frozen 5-fold x 10-repeat event-stratified assignments."""
    seed_base = _integer(resampling_spec.get("outer_seed_base"), "outer_seed_base")
    repeat_n = _integer(resampling_spec.get("repeat_n"), "repeat_n")
    fold_n = _integer(resampling_spec.get("fold_n"), "fold_n")
    if repeat_n != 10 or fold_n != 5:
        raise ValueError("Task04 requires exactly 5 folds and 10 repeats")
    parsed: list[tuple[int, int, int]] = []
    for row in index_outcome:
        sid, hid = _canonical_identity(row)
        event = _integer(row.get("death_180d"), "death_180d")
        if event not in (0, 1):
            raise ValueError("death_180d must be binary")
        parsed.append((sid, hid, event))
    if len(parsed) != len({(sid, hid) for sid, hid, _ in parsed}):
        raise ValueError("duplicate development identity")
    if len(parsed) != 2839 or sum(event for _, _, event in parsed) != 435:
        raise ValueError("development fold population must be N=2839 with 435 events")

    assignments: dict[tuple[int, int], dict[str, str]] = {
        (sid, hid): {"subject_id": str(sid), "hadm_id": str(hid)}
        for sid, hid, _ in parsed
    }
    for repeat in range(1, repeat_n + 1):
        seed = seed_base + repeat - 1
        start_fold = ((repeat - 1) % fold_n) + 1
        for event in (0, 1):
            stratum = [(sid, hid) for sid, hid, outcome in parsed if outcome == event]
            def sort_key(identity: tuple[int, int]) -> tuple[bytes, int, int]:
                sid, hid = identity
                payload = f"{seed}|{event}|{sid}|{hid}".encode("utf-8")
                return hashlib.sha256(payload).digest(), sid, hid
            for position, identity in enumerate(sorted(stratum, key=sort_key)):
                fold = ((start_fold - 1 + position) % fold_n) + 1
                assignments[identity][f"repeat_{repeat}_fold"] = str(fold)

    rows = [assignments[key] for key in sorted(assignments)]
    for repeat in range(1, repeat_n + 1):
        events = {fold: 0 for fold in range(1, fold_n + 1)}
        sizes = {fold: 0 for fold in range(1, fold_n + 1)}
        outcome_by_id = {(sid, hid): event for sid, hid, event in parsed}
        for row in rows:
            identity = (int(row["subject_id"]), int(row["hadm_id"]))
            fold = int(row[f"repeat_{repeat}_fold"])
            if fold not in sizes:
                raise AssertionError("invalid fold number")
            sizes[fold] += 1
            events[fold] += outcome_by_id[identity]
        if set(events.values()) != {87} or set(sizes.values()) - {567, 568}:
            raise AssertionError("frozen fold invariants failed")
    return rows


def build_temporal_identity_seal(full_identity_stream: Iterable[Mapping[str, Any]],
                                 development_ids: set[tuple[str, str]],
                                 expected_n: int = 406) -> list[dict[str, str]]:
    """Create an identity-only, development-disjoint temporal seal."""
    dev = {(_integer(sid, "subject_id"), _integer(hid, "hadm_id")) for sid, hid in development_ids}
    temporal: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    for source in full_identity_stream:
        row = dict(source)
        if tuple(row) != IDENTITY_FIELDS or set(row) != set(IDENTITY_FIELDS):
            raise ValueError("temporal seal source must be identity-only")
        identity = _canonical_identity(row)
        if identity in seen:
            raise ValueError("duplicate full-cohort identity")
        seen.add(identity)
        if identity not in dev:
            temporal.append(identity)
    if not dev.issubset(seen) or set(temporal) & dev or len(temporal) != expected_n:
        raise ValueError("temporal identity seal count/disjointness failed")
    return [{"subject_id": str(sid), "hadm_id": str(hid)} for sid, hid in sorted(temporal)]


class FoldLocalPreprocessingGuard:
    """Tiny authorization guard used by all later preprocessing objects."""

    _FIT_SCOPES = {"outer_training", "inner_training"}

    def __init__(self) -> None:
        self.fitted_scope: str | None = None
        self.fit_index_count: int | None = None

    def fit(self, indices: Iterable[Any], *, scope: str) -> str:
        if scope not in self._FIT_SCOPES:
            raise PermissionError("preprocessing fit is restricted to training partitions")
        values = list(indices)
        if not values:
            raise ValueError("training indices cannot be empty")
        self.fitted_scope = scope
        self.fit_index_count = len(values)
        return "FIT_ALLOWED"

    def transform(self, indices: Iterable[Any], *, scope: str) -> str:
        if self.fitted_scope is None:
            raise RuntimeError("preprocessor has not been fit")
        if scope not in {"inner_validation", "outer_test", "temporal_validation", *self._FIT_SCOPES}:
            raise PermissionError("unknown transform scope")
        list(indices)
        return "TRANSFORM_ALLOWED"


def evaluate_complexity_gate(receipt: Mapping[str, Any], environment: Mapping[str, Any],
                             envelope: Mapping[str, Any] | None = None) -> str:
    """Evaluate the pre-A5 objective resource envelope without fitting a model."""
    envelope = dict(envelope or {})
    physical = int(environment["physical_ram_bytes"])
    free_start = int(environment["start_free_disk_bytes"])
    logical = int(environment["logical_cpu_count"])
    limits = {
        "rss": min(int(envelope.get("max_rss_bytes", 16 * 1024**3)), physical // 2),
        "disk": min(int(envelope.get("max_task_disk_bytes", 20 * 1024**3)), free_start // 4),
        "workers": min(int(envelope.get("max_workers", 4)), logical),
        "wall": int(envelope.get("max_wall_seconds", 8 * 60 * 60)),
        "end_free": int(envelope.get("min_end_free_disk_bytes", 20 * 1024**3)),
    }
    required_equal = {
        "signal_n": 41, "extension_column_n": 157, "candidate_width": 177,
        "outer_training_event_n": 348, "patient_n": 2839, "training_deletion_n": 0,
        "temporal_value_access_n": 0,
    }
    ok = all(int(receipt.get(key, -1)) == value for key, value in required_equal.items())
    ok &= str(receipt.get("fit_count_status")) == "EXACT_FIT_COUNT_PENDING_A5"
    ok &= int(receipt.get("peak_rss_bytes", limits["rss"] + 1)) <= limits["rss"]
    ok &= int(receipt.get("peak_task_disk_bytes", limits["disk"] + 1)) <= limits["disk"]
    ok &= int(receipt.get("end_free_disk_bytes", -1)) >= limits["end_free"]
    ok &= 0 < int(receipt.get("max_workers", limits["workers"] + 1)) <= limits["workers"]
    ok &= 0 <= float(receipt.get("wall_seconds", limits["wall"] + 1)) <= limits["wall"]
    return "PASS_TO_A5" if ok else "HOLD"


def validate_field_dictionary(rows: Iterable[Mapping[str, Any]],
                              expected_columns_by_table: Mapping[str, list[str]]) -> dict[str, Any]:
    """Enforce exact physical separation and column identity for the three tables."""
    observed = [dict(row) for row in rows]
    by_table: dict[str, list[dict[str, Any]]] = {table: [] for table in expected_columns_by_table}
    seen: set[tuple[str, str]] = set()
    for row in observed:
        table = str(row.get("table_name", "")); column = str(row.get("column_name", ""))
        if table not in by_table or (table, column) in seen:
            raise ValueError("unknown or duplicate field dictionary entry")
        seen.add((table, column)); by_table[table].append(row)
        role = str(row.get("role", "")); scope = str(row.get("allowed_model_scope", ""))
        if role not in ALLOWED_ROLES:
            raise ValueError("unknown field role")
        if table == "DEVELOPMENT_INDEX_OUTCOME" and role not in {"identity", "outcome", "audit_only"}:
            raise ValueError("index/outcome table contains predictors")
        if table == "DEVELOPMENT_C2_RAW" and role not in {"identity", "core_raw"}:
            raise ValueError("C2 table contains a non-core field")
        if table == "DEVELOPMENT_EXTENDED_157":
            if role == "identity":
                if scope != "joining_only":
                    raise ValueError("extension identity scope drift")
            elif role not in {"extension_numeric", "extension_binary",
                              "extension_unavailable", "extension_freshness"} or scope != "M2":
                raise ValueError("extension table scope/role drift")
        if role == "outcome" and scope != "folding_only":
            raise ValueError("outcome may only be used for folding/evaluation")
    for table, expected in expected_columns_by_table.items():
        actual_rows = sorted(by_table[table], key=lambda row: _integer(row["column_order"], "column_order"))
        actual = [str(row["column_name"]) for row in actual_rows]
        if actual != list(expected) or [int(row["column_order"]) for row in actual_rows] != list(range(1, len(expected) + 1)):
            raise ValueError(f"field dictionary mismatch for {table}")
    if len(observed) != sum(len(columns) for columns in expected_columns_by_table.values()):
        raise ValueError("field dictionary has extra or missing rows")
    return {"status": "PASS", "field_n": len(observed), "table_n": len(by_table)}


def _safe_relative(root: Path, relative: str) -> Path:
    pure = Path(relative)
    if pure.is_absolute() or relative.replace("\\", "/").startswith("../") or ".." in pure.parts:
        raise PermissionError("artifact path escapes Project_B")
    path = (root / pure).resolve(strict=False)
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise PermissionError("artifact path escapes Project_B") from exc
    return path


def verify_exact_manifest(project_root: str | Path, manifest_rows: Iterable[Mapping[str, Any]],
                          expected_artifacts: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Verify the manifest is an exact, unique, non-self-referential file set."""
    root = Path(project_root).resolve(strict=False)
    rows = [dict(row) for row in manifest_rows]
    expected = [dict(row) for row in expected_artifacts]
    row_paths = [str(row.get("relative_path", "")) for row in rows]
    expected_paths = [str(row.get("relative_path", "")) for row in expected]
    if len(row_paths) != len(set(row_paths)) or len(expected_paths) != len(set(expected_paths)):
        raise ValueError("manifest path set is not unique")
    if set(row_paths) != set(expected_paths):
        raise ValueError("manifest path set differs from frozen expectation")
    expected_by_path = {str(row["relative_path"]): row for row in expected}
    for row in rows:
        relative = str(row["relative_path"])
        if Path(relative).name == "A4_FREEZE_MANIFEST.csv":
            raise ValueError("manifest cannot contain itself")
        target = _safe_relative(root, relative)
        if not target.is_file():
            raise ValueError(f"manifest target missing: {relative}")
        contract = expected_by_path[relative]
        for key in ("role", "access_class"):
            if str(row.get(key, "")) != str(contract.get(key, "")):
                raise ValueError(f"manifest {key} mismatch: {relative}")
        if int(row.get("bytes", -1)) != target.stat().st_size or str(row.get("sha256", "")).upper() != _sha256(target):
            raise ValueError(f"manifest content mismatch: {relative}")
        if str(row.get("status", "")) != "SEALED":
            raise ValueError(f"manifest status mismatch: {relative}")
    return {"status": "PASS", "artifact_n": len(rows)}


def validate_temporal_materialization_spec(spec: Mapping[str, Any], a4_manifest: Iterable[Mapping[str, Any]],
                                           temporal_seal_rows: Iterable[Mapping[str, Any]], *,
                                           root: str | Path) -> dict[str, Any]:
    """Validate the still-dormant authorization-first materialization plan."""
    required_top = {
        "schema_version", "status", "authorized", "attempt_count", "compression_branch",
        "temporal_identity_seal", "future_tables", "future_audit_artifacts", "raw_value_handling",
        "frozen_source_chain", "authorization_prerequisites", "one_time_execution",
        "forbidden_before_authorization",
    }
    if set(spec) != required_top:
        raise ValueError("temporal materialization spec top-level schema drift")
    if (spec["status"], spec["authorized"], int(spec["attempt_count"]), spec["compression_branch"]) != (
            "SEALED_IDENTITY_ONLY", False, 0, "PENDING"):
        raise ValueError("temporal materialization must remain pending and unauthorized")
    if str(spec.get("raw_value_handling", {}).get("access_class")) != "ephemeral_no_persist":
        raise ValueError("temporal raw values must not persist")
    raw = spec.get("raw_value_handling", {})
    if (raw.get("streaming_mode") != "single_pass_bounded_memory"
            or int(raw.get("validation_chunk_rows", -1)) != 2048
            or raw.get("raw_disk_spool_allowed") is not False):
        raise ValueError("temporal raw stream must use the frozen bounded no-spool contract")
    prerequisite_roles = {str(item.get("receipt_role", ""))
                          for item in spec.get("authorization_prerequisites", [])}
    if not {"A4_FREEZE_MANIFEST_SHA256", "TEMPORAL_MATERIALIZATION_SPEC_SHA256"} <= prerequisite_roles:
        raise ValueError("temporal authorization lacks the A4 manifest/spec anchors")
    execution = spec.get("one_time_execution", {})
    if int(execution.get("max_attempts", -1)) != 1:
        raise ValueError("temporal materialization must be one-time")
    if (execution.get("atomic_write_rule") != "stage_all_verify_hashes_publish_set_or_rollback"
            or execution.get("failure_visibility_rule") != "failed_log_only"
            or execution.get("claim_path") != "audit/temporal/.TEMPORAL_MATERIALIZATION.claim"
            or execution.get("claim_create_rule") != "atomic_create_exclusive_before_value_source"
            or execution.get("claim_release_rule") != "release_after_handled_success_or_failure"
            or execution.get("stale_claim_rule") != "never_auto_remove_manual_adjudication"
            or execution.get("attempt_id_rule") != "uuid4_hex_bound_to_claim_and_all_logs"
            or execution.get("preclaim_read_rule") != "authorization_metadata_manifest_spec_identity_seal_only"
            or execution.get("postclaim_value_source_rule") != "claim_owner_confirmed_before_hash_or_open"):
        raise ValueError("temporal artifact-set transaction contract drift")
    seal_rows = [dict(row) for row in temporal_seal_rows]
    if not seal_rows or any(tuple(row) != IDENTITY_FIELDS for row in seal_rows):
        raise ValueError("temporal seal is not identity-only")
    project_root = Path(root).resolve(strict=False)
    seal_info = spec["temporal_identity_seal"]
    seal_path = _safe_relative(project_root, str(seal_info["path"]))
    if not seal_path.is_file() or _sha256(seal_path) != str(seal_info["sha256"]).upper():
        raise ValueError("temporal seal hash mismatch")
    future = list(spec["future_tables"]) + list(spec["future_audit_artifacts"])
    if len(spec["future_tables"]) != FUTURE_TABLE_N or len(spec["future_audit_artifacts"]) != FUTURE_AUDIT_N:
        raise ValueError("temporal future artifact count drift")
    paths = [str(item["path"]) for item in future]
    if len(paths) != len(set(paths)):
        raise ValueError("duplicate temporal future path")
    for relative in paths:
        if _safe_relative(project_root, relative).exists():
            raise ValueError("future temporal artifact already exists")
    manifest_by_path = {str(row["relative_path"]): row for row in a4_manifest}
    for item in spec["frozen_source_chain"]:
        relative = str(item["path"])
        if manifest_by_path:
            manifest = manifest_by_path.get(relative)
            if not manifest or str(manifest.get("sha256", "")).upper() != str(item["sha256"]).upper():
                raise ValueError("temporal frozen source chain differs from A4 manifest")
    return {"status": "SEALED_IDENTITY_ONLY", "temporal_n": len(seal_rows),
            "future_artifact_n": len(future), "authorized": False}


def _write_csv(path: Path, fieldnames: list[str], rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader(); writer.writerows(rows)
    os.replace(temporary, path)


def freeze_a4_bundle(project_root: str | Path, *, fold_rows: Iterable[Mapping[str, Any]] | None = None,
                     temporal_rows: Iterable[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Persist only explicitly supplied, already-validated A4 deterministic tables."""
    root = Path(project_root).resolve(strict=False)
    if root.name != "Project_B" or any(part.casefold() == "project_a" for part in root.parts):
        raise PermissionError("canonical Project_B root required")
    written: list[str] = []
    if fold_rows is not None:
        rows = list(fold_rows)
        _write_csv(root / "data" / "a4" / "OUTER_FOLD_ASSIGNMENTS.csv",
                   ["subject_id", "hadm_id", *[f"repeat_{n}_fold" for n in range(1, 11)]], rows)
        written.append("data/a4/OUTER_FOLD_ASSIGNMENTS.csv")
    if temporal_rows is not None:
        rows = list(temporal_rows)
        _write_csv(root / "data" / "a4" / "TEMPORAL_ID_SEAL.csv", list(IDENTITY_FIELDS), rows)
        written.append("data/a4/TEMPORAL_ID_SEAL.csv")
    return {"status": "WRITTEN", "relative_paths": written}
