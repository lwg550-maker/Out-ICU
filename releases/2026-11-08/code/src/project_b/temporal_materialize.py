"""Authorization-first, one-shot temporal materialization.

Task04 freezes this module but never executes it against the temporal cohort.
The public entrypoint has no injectable value-source argument.  After the
authorization and A4-manifest guards pass, the frozen A3 runner is consumed in
one bounded pass.  Patient-level raw rows are never written to disk.
"""

from __future__ import annotations

from bisect import bisect_left
from collections import Counter, defaultdict
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
from types import ModuleType
from typing import Any, Iterable, Mapping
import uuid

from .a3_values import (
    C2_RAW_FIELDS,
    VALUE_FIELDS,
    build_c2_raw,
    build_extended_matrix,
    guarded_master_rows,
    initial_gcs_assessable,
    validate_value_extract,
)


INDEX_FIELDS = [
    "subject_id", "hadm_id", "episode_start", "t0", "admin_90_eligible",
    "admin_180_eligible", "death_90d", "death_180d", "death_days_after_t0",
]
REQUIRED_SOURCE_ROLES = {
    "frozen_master", "p004_matrix_spec", "a3_sql", "a3_sql_hash_manifest",
    "readonly_runner", "a1_registry", "working_registry", "source_contracts",
    "representation", "value_extraction_spec", "clinical_domain_map",
    "field_dictionary", "aggregation_code", "temporal_module",
}
EXPECTED_FUTURE_TABLES = {
    "data/temporal/TEMPORAL_INDEX_OUTCOME.csv",
    "data/temporal/TEMPORAL_C2_RAW.csv",
    "data/temporal/TEMPORAL_EXTENDED_157.csv",
}
EXPECTED_FUTURE_AUDITS = {
    "audit/temporal/TEMPORAL_RAW_STREAM.readonly.json",
    "audit/temporal/TEMPORAL_SOURCE_COUNTS.csv",
    "audit/temporal/TEMPORAL_AGGREGATION_RECEIPT.json",
    "audit/temporal/TEMPORAL_UNSEAL_LOG.json",
    "audit/temporal/TEMPORAL_MATERIALIZATION_RECEIPT.json",
}
CORE_AUDITS = {
    "audit/temporal/TEMPORAL_RAW_STREAM.readonly.json",
    "audit/temporal/TEMPORAL_SOURCE_COUNTS.csv",
    "audit/temporal/TEMPORAL_AGGREGATION_RECEIPT.json",
}
ALL_FUTURE_ARTIFACTS = EXPECTED_FUTURE_TABLES | EXPECTED_FUTURE_AUDITS
VALIDATION_CHUNK_ROWS = 2048
MAX_NEURO_CANDIDATES_PER_IDENTITY = 4096
_BINARY_VARIABLES = {
    "rrt_ever", "rrt_active_t0", "hyperosmolar_ever", "sedative_active_t0",
    "nmb_active_t0", "vasopressor_active_t0", "invasive_vent_active_t0",
}
_STOP_VARIABLES = {"hours_since_vasopressor_end", "hours_since_invasive_vent_end"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _json_digest(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest().upper()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise PermissionError(f"authorization prerequisite missing: {path.name}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise PermissionError("authorization document must be an object")
    return value


def _inside(root: Path, relative: str, role: str) -> Path:
    path = (root / relative).resolve(strict=False)
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise PermissionError(f"{role} path escapes Project_B") from exc
    return path


def _verify_bound_file(root: Path, binding: Any, role: str, *, expected_path: str | None = None) -> Path:
    if not isinstance(binding, dict) or set(binding) != {"path", "sha256"}:
        raise PermissionError(f"{role} file binding schema is incomplete")
    relative = str(binding["path"])
    if expected_path is not None and relative != expected_path:
        raise PermissionError(f"{role} file binding path drift")
    path = _inside(root, relative, role)
    expected = str(binding["sha256"]).upper()
    if len(expected) != 64 or not path.is_file() or _sha256(path) != expected:
        raise PermissionError(f"{role} file binding does not match")
    return path


def _validate_bound_file_metadata(root: Path, binding: Any, role: str) -> dict[str, str]:
    """Validate a binding without opening, statting or hashing its target."""
    if not isinstance(binding, dict) or set(binding) != {"path", "sha256"}:
        raise PermissionError(f"{role} file binding schema is incomplete")
    relative = str(binding["path"])
    _inside(root, relative, role)
    expected = str(binding["sha256"]).upper()
    if len(expected) != 64 or any(character not in "0123456789ABCDEF" for character in expected):
        raise PermissionError(f"{role} file binding hash is invalid")
    return {"path": relative, "sha256": expected}


def _manifest_spec_anchor(root: Path, receipt: dict[str, Any]) -> tuple[Path, Path, str, str]:
    manifest_path = _verify_bound_file(
        root, receipt.get("a4_manifest"), "A4 manifest",
        expected_path="manifests/A4_FREEZE_MANIFEST.csv")
    spec_path = _verify_bound_file(
        root, receipt.get("temporal_materialization_spec"), "temporal materialization spec",
        expected_path="config/TEMPORAL_MATERIALIZATION_SPEC.json")
    manifest_sha = _sha256(manifest_path); spec_sha = _sha256(spec_path)
    with manifest_path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    matches = [row for row in rows
               if row.get("relative_path") == "config/TEMPORAL_MATERIALIZATION_SPEC.json"]
    if len(matches) != 1:
        raise PermissionError("A4 manifest must contain exactly one temporal spec anchor")
    anchored = matches[0]
    if (str(anchored.get("sha256", "")).upper() != spec_sha
            or str(anchored.get("status", "")) != "SEALED"
            or int(anchored.get("bytes", -1)) != spec_path.stat().st_size):
        raise PermissionError("temporal spec differs from the sealed A4 manifest")
    return manifest_path, spec_path, manifest_sha, spec_sha


def _read_identity_seal(root: Path, spec: dict[str, Any]) -> tuple[Path, list[dict[str, str]]]:
    seal_info = spec.get("temporal_identity_seal", {})
    seal_path = _inside(root, str(seal_info.get("path", "")), "temporal identity seal")
    if not seal_path.is_file() or _sha256(seal_path) != str(seal_info.get("sha256", "")).upper():
        raise PermissionError("temporal identity seal hash mismatch")
    with seal_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if list(reader.fieldnames or []) != ["subject_id", "hadm_id"]:
            raise PermissionError("temporal identity seal is not identity-only")
        rows = list(reader)
    identities = [(row["subject_id"], row["hadm_id"]) for row in rows]
    if len(rows) != 406 or len(set(identities)) != 406:
        raise PermissionError("temporal identity count mismatch")
    return seal_path, rows


def _authorization_preflight(
    root: Path, receipt_path: Path,
) -> tuple[dict[str, Any], dict[str, Any], Path, list[dict[str, str]], dict[str, Path], dict[str, str]]:
    if root.name != "Project_B" or any(part.casefold() == "project_a" for part in root.parts):
        raise PermissionError("canonical Project_B root required")
    try:
        receipt_path.relative_to(root)
    except ValueError as exc:
        raise PermissionError("authorization receipt must be inside Project_B") from exc

    # Only authorization metadata and the A4 immutable anchors are read before
    # the sealed temporal specification and identity-only seal.
    receipt = _load_json(receipt_path)
    operator = str(receipt.get("operator", "")).strip()
    if not operator:
        raise PermissionError("authorization receipt operator is required")
    manifest_path, spec_path, manifest_sha, spec_sha = _manifest_spec_anchor(root, receipt)
    spec = _load_json(spec_path)
    if (spec.get("status") != "SEALED_IDENTITY_ONLY" or spec.get("authorized") is not False
            or int(spec.get("attempt_count", -1)) != 0 or spec.get("compression_branch") != "PENDING"):
        raise PermissionError("temporal seal is not pristine")
    if int(spec.get("one_time_execution", {}).get("max_attempts", -1)) != 1:
        raise PermissionError("temporal materialization is not one-time")
    raw = spec.get("raw_value_handling", {})
    if (raw.get("streaming_mode") != "single_pass_bounded_memory"
            or int(raw.get("validation_chunk_rows", -1)) != VALIDATION_CHUNK_ROWS
            or raw.get("raw_disk_spool_allowed") is not False):
        raise PermissionError("temporal raw stream contract is not bounded and no-spool")
    prerequisite_roles = {str(item.get("receipt_role", ""))
                          for item in spec.get("authorization_prerequisites", [])}
    if not {"A4_FREEZE_MANIFEST_SHA256", "TEMPORAL_MATERIALIZATION_SPEC_SHA256"} <= prerequisite_roles:
        raise PermissionError("temporal authorization anchor prerequisites are missing")
    execution_contract = spec.get("one_time_execution", {})
    if (execution_contract.get("atomic_write_rule") != "stage_all_verify_hashes_publish_set_or_rollback"
            or execution_contract.get("failure_visibility_rule") != "failed_log_only"
            or execution_contract.get("claim_path") != "audit/temporal/.TEMPORAL_MATERIALIZATION.claim"
            or execution_contract.get("claim_create_rule") != "atomic_create_exclusive_before_value_source"
            or execution_contract.get("claim_release_rule") != "release_after_handled_success_or_failure"
            or execution_contract.get("stale_claim_rule") != "never_auto_remove_manual_adjudication"
            or execution_contract.get("attempt_id_rule") != "uuid4_hex_bound_to_claim_and_all_logs"
            or execution_contract.get("preclaim_read_rule") != "authorization_metadata_manifest_spec_identity_seal_only"
            or execution_contract.get("postclaim_value_source_rule") != "claim_owner_confirmed_before_hash_or_open"):
        raise PermissionError("temporal artifact-set transaction contract drift")
    branch = receipt.get("compression_branch")
    if branch not in {"NOT_LAUNCHED", "LAUNCHED_AND_LOCKED"}:
        raise PermissionError("compression branch is pending or illegal")
    if (receipt.get("authorized") is not True or receipt.get("a5_status") != "PASS"
            or receipt.get("nested_development_complete") is not True):
        raise PermissionError("A5 and nested development completion are not authorized")
    seal_path, seal_rows = _read_identity_seal(root, spec)

    table_items = spec.get("future_tables")
    audit_items = spec.get("future_audit_artifacts")
    if (not isinstance(table_items, list) or not isinstance(audit_items, list)
            or {str(item.get("path")) for item in table_items} != EXPECTED_FUTURE_TABLES
            or {str(item.get("path")) for item in audit_items} != EXPECTED_FUTURE_AUDITS):
        raise PermissionError("future temporal artifact set drift")
    future_paths = [_inside(root, str(item["path"]), "future temporal")
                    for item in table_items + audit_items]
    if any(path.exists() for path in future_paths):
        raise PermissionError("temporal output or execution artifact already exists")

    objects = receipt.get("final_development_objects")
    required_objects = {"M0", "M1", "M2"}
    required_model_roles = {"model", "preprocessor", "baseline", "native_calibrator", "supportive_calibrator"}
    if not isinstance(objects, dict) or set(objects) != required_objects:
        raise PermissionError("final M0/M1/M2 object bindings are incomplete")
    for model in sorted(required_objects):
        if not isinstance(objects[model], dict) or set(objects[model]) != required_model_roles:
            raise PermissionError(f"{model} final object roles incomplete")
    object_bindings = {
        model: {role: _validate_bound_file_metadata(root, item, f"{model}:{role}")
                for role, item in objects[model].items()}
        for model in sorted(required_objects)
    }
    final_bindings = receipt.get("final_bindings")
    required_bindings = {"field_schema", "code", "sql", "state"}
    if not isinstance(final_bindings, dict) or set(final_bindings) != required_bindings:
        raise PermissionError("final schema/code/SQL/state bindings are incomplete")
    normalized_final_bindings = {
        role: _validate_bound_file_metadata(root, item, f"final:{role}")
        for role, item in final_bindings.items()
    }
    compact_bindings: dict[str, dict[str, str]] = {}
    if branch == "LAUNCHED_AND_LOCKED":
        compact = receipt.get("compact_model")
        required = {"variables", "representation", "algorithm", "hyperparameters", "preprocessor",
                    "baseline", "calibrator", "absolute_risk", "missingness"}
        if not isinstance(compact, dict) or set(compact) != required:
            raise PermissionError("locked compact-model specification incomplete")
        compact_bindings = {
            role: _validate_bound_file_metadata(root, item, f"compact:{role}")
            for role, item in compact.items()
        }

    source_items = spec.get("frozen_source_chain")
    if not isinstance(source_items, list):
        raise PermissionError("frozen source chain missing")
    roles = [str(item.get("role", "")) for item in source_items]
    if len(roles) != len(set(roles)) or set(roles) != REQUIRED_SOURCE_ROLES:
        raise PermissionError("frozen source-chain role set drift")
    source_bindings = {
        role: _validate_bound_file_metadata(
            root, {"path": item.get("path"), "sha256": item.get("sha256")}, f"frozen:{role}")
        for role, item in zip(roles, source_items)
    }
    binding_plan = {
        "final_development_objects": object_bindings,
        "final_bindings": normalized_final_bindings,
        "compact_model": compact_bindings,
        "frozen_sources": source_bindings,
    }
    anchors = {
        "a4_manifest_path": manifest_path.relative_to(root).as_posix(),
        "a4_manifest_sha256": manifest_sha,
        "temporal_materialization_spec_path": spec_path.relative_to(root).as_posix(),
        "temporal_materialization_spec_sha256": spec_sha,
    }
    return spec, receipt, seal_path, seal_rows, binding_plan, anchors


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _acquire_claim(path: Path, payload: dict[str, Any]) -> None:
    """Atomically claim the one permitted run before any value-bearing source opens."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise PermissionError("temporal materialization is already claimed") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
            handle.flush(); os.fsync(handle.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _assert_claim_owner(path: Path, attempt_id: str) -> dict[str, Any]:
    if not path.is_file():
        raise PermissionError("temporal materialization claim is missing")
    claim = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(claim, dict) or claim.get("attempt_id") != attempt_id
            or claim.get("claim_status") != "RUNNING"):
        raise PermissionError("temporal materialization claim ownership mismatch")
    return claim


def _release_claim(path: Path, attempt_id: str) -> None:
    _assert_claim_owner(path, attempt_id)
    path.unlink()


def _verify_claimed_value_sources(
    root: Path, binding_plan: dict[str, Any], claim_path: Path, attempt_id: str,
) -> dict[str, Path]:
    """Hash all potentially value-bearing bindings only while this attempt owns the claim."""
    _assert_claim_owner(claim_path, attempt_id)
    for model, bindings in binding_plan["final_development_objects"].items():
        for role, item in bindings.items():
            _assert_claim_owner(claim_path, attempt_id)
            _verify_bound_file(root, item, f"{model}:{role}")
    for role, item in binding_plan["final_bindings"].items():
        _assert_claim_owner(claim_path, attempt_id)
        _verify_bound_file(root, item, f"final:{role}")
    for role, item in binding_plan["compact_model"].items():
        _assert_claim_owner(claim_path, attempt_id)
        _verify_bound_file(root, item, f"compact:{role}")
    sources: dict[str, Path] = {}
    for role, item in binding_plan["frozen_sources"].items():
        _assert_claim_owner(claim_path, attempt_id)
        sources[role] = _verify_bound_file(root, item, f"frozen:{role}")
    return sources


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _write_csv(path: Path, fields: list[str], rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n", extrasaction="raise")
        writer.writeheader(); writer.writerows(rows)


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), list(reader)


def _load_frozen_runner(path: Path) -> ModuleType:
    module_spec = importlib.util.spec_from_file_location("project_b_frozen_temporal_runner", path)
    if module_spec is None or module_spec.loader is None:
        raise RuntimeError("frozen runner cannot be loaded")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    if not callable(getattr(module, "iter_readonly_rows", None)):
        raise RuntimeError("frozen runner lacks iter_readonly_rows")
    return module


def _dt(value: Any) -> datetime | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _float(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _truth(value: Any) -> bool:
    return value is True or value == "t"


def _candidate_time_key(row: Mapping[str, Any]) -> tuple[datetime, datetime, str]:
    event = _dt(row.get("event_time")); available = _dt(row.get("availability_time"))
    if event is None or available is None:
        raise RuntimeError("included temporal row has invalid event/availability time")
    return event, available, str(row.get("record_key", ""))


class _BoundedRepresentativeRows:
    """Keep only sufficient statistics/representatives for the frozen 157-column builder."""

    def __init__(self, representation: list[dict[str, str]]) -> None:
        self.columns: dict[str, list[str]] = defaultdict(list)
        for item in representation:
            self.columns[item["variable_id"]].append(item["column_name"])
        self.fixed: dict[tuple[str, str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
        self.gcs: dict[tuple[str, str], dict[str, dict[str, dict[str, Any]]]] = defaultdict(dict)
        self.pupil: dict[tuple[str, str], dict[str, list[dict[str, Any]]]] = defaultdict(
            lambda: {"227288": [], "227121": []})

    def add(self, source: Mapping[str, Any]) -> None:
        if not _truth(source.get("included")):
            return
        row = dict(source)
        identity = (str(row["subject_id"]), str(row["hadm_id"]))
        variable = str(row["variable_id"]); key = (*identity, variable)
        if variable in _BINARY_VARIABLES:
            self.fixed[key].setdefault("one", row)
            return
        if variable in _STOP_VARIABLES:
            previous = self.fixed[key].get("latest")
            if previous is None or _candidate_time_key(row) > _candidate_time_key(previous):
                self.fixed[key]["latest"] = row
            return
        if variable == "initial_gcs_total":
            number = _float(row.get("raw_numeric_value"))
            if number is None or not initial_gcs_assessable(row.get("status_text")):
                return
            event = str(row["event_time"]); itemid = str(row["itemid"])
            times = self.gcs[identity]
            if event not in times and len(times) >= MAX_NEURO_CANDIDATES_PER_IDENTITY:
                raise RuntimeError("bounded initial-GCS candidate envelope exceeded")
            item_rows = times.setdefault(event, {})
            previous = item_rows.get(itemid)
            if (previous is None or number < float(previous["raw_numeric_value"])
                    or (number == float(previous["raw_numeric_value"])
                        and str(row["record_key"]) < str(previous["record_key"]))):
                item_rows[itemid] = row
            return
        if variable == "initial_pupil_nonreactive_count":
            itemid = str(row["itemid"])
            if itemid not in self.pupil[identity]:
                return
            sides = self.pupil[identity]
            if sum(len(values) for values in sides.values()) >= 2 * MAX_NEURO_CANDIDATES_PER_IDENTITY:
                raise RuntimeError("bounded initial-pupil candidate envelope exceeded")
            sides[itemid].append(row)
            return
        number = _float(row.get("raw_numeric_value"))
        if number is None or not self.columns.get(variable):
            return
        event, available, record_key = _candidate_time_key(row)
        episode_start = _dt(row.get("episode_start")); t0 = _dt(row.get("t0"))
        if episode_start is None or t0 is None:
            raise RuntimeError("included trajectory row has invalid episode boundary")
        state = self.fixed[key]
        if event < min(episode_start + timedelta(hours=24), t0):
            previous = state.get("early")
            if previous is None or (event, available, record_key) < _candidate_time_key(previous):
                state["early"] = row
        if event >= max(episode_start, t0 - timedelta(hours=48)):
            previous = state.get("last")
            if previous is None or (event, available, record_key) > _candidate_time_key(previous):
                state["last"] = row
        previous = state.get("low")
        if previous is None or (number, event, record_key) < (
                float(previous["raw_numeric_value"]), _dt(previous["event_time"]), str(previous["record_key"])):
            state["low"] = row
        previous = state.get("high")
        if previous is None or (number, event, record_key) > (
                float(previous["raw_numeric_value"]), _dt(previous["event_time"]), str(previous["record_key"])):
            state["high"] = row

    @staticmethod
    def _pupil_pair(sides: dict[str, list[dict[str, Any]]]) -> tuple[dict[str, Any], dict[str, Any]] | None:
        def event_representatives(rows: list[dict[str, Any]]) -> list[tuple[datetime, dict[str, Any]]]:
            # The frozen builder ignores availability_time for pupil pairing.
            # At one event_time only the lexicographically smallest record_key
            # on a side can win its exact ordering rule.
            by_event: dict[datetime, dict[str, Any]] = {}
            for row in rows:
                event = _dt(row.get("event_time"))
                if event is None:
                    raise RuntimeError("included pupil row has invalid event_time")
                previous = by_event.get(event)
                if previous is None or str(row["record_key"]) < str(previous["record_key"]):
                    by_event[event] = row
            return sorted(by_event.items(), key=lambda item: (item[0], str(item[1]["record_key"])))

        left = event_representatives(sides["227288"])
        right = event_representatives(sides["227121"])
        if not left or not right:
            return None
        right_times = [event for event, _row in right]
        best = None
        for ltime, lrow in left:
            # For a fixed left record, the earliest right event in the allowed
            # window minimizes min(event_time); if it is not earlier than the
            # left event it also minimizes the time difference.
            index = bisect_left(right_times, ltime - timedelta(minutes=30))
            if index >= len(right):
                continue
            rtime, rrow = right[index]
            delta = abs((ltime - rtime).total_seconds())
            if delta > 1800:
                continue
            candidate = (min(ltime, rtime), delta, str(lrow["record_key"]),
                         str(rrow["record_key"]), lrow, rrow)
            if best is None or candidate[:4] < best[:4]:
                best = candidate
        return None if best is None else (best[4], best[5])

    def representatives(self) -> list[dict[str, Any]]:
        representatives: dict[tuple[str, str], dict[str, Any]] = {}
        for states in self.fixed.values():
            for row in states.values():
                representatives[(str(row["source_contract_id"]), str(row["record_key"]))] = row
        required = {"220739", "223900", "223901"}
        for times in self.gcs.values():
            complete = [(event, rows) for event, rows in times.items() if required <= set(rows)]
            if complete:
                _event, rows = min(complete, key=lambda item: _dt(item[0]) or datetime.max)
                for itemid in sorted(required):
                    row = rows[itemid]
                    representatives[(str(row["source_contract_id"]), str(row["record_key"]))] = row
        for sides in self.pupil.values():
            pair = self._pupil_pair(sides)
            if pair:
                for row in pair:
                    representatives[(str(row["source_contract_id"]), str(row["record_key"]))] = row
        return list(representatives.values())


class _BoundedValidation:
    def __init__(self, contracts: list[dict[str, str]], registry: dict[str, dict[str, str]],
                 allowed: set[tuple[str, str]]) -> None:
        self.contracts = contracts; self.registry = registry; self.allowed = allowed
        self.contract_specs = {row["source_contract_id"]: row for row in contracts}
        self.expected = {row["source_contract_id"]: {"record_count": 0, "included_count": 0}
                         for row in contracts}
        self.counts = {key: [0, 0, 0] for key in self.expected}
        self.exclusions: Counter[tuple[str, str]] = Counter()
        self.warnings: Counter[tuple[str, str]] = Counter()
        self.patients: set[tuple[str, str]] = set()
        self.previous_sort_key: tuple[str, str] | None = None
        self.record_count = 0

    def validate_chunk(self, chunk: list[dict[str, str]]) -> None:
        if not chunk:
            return
        for row in chunk:
            sort_key = (str(row.get("source_contract_id", "")), str(row.get("record_key", "")))
            if self.previous_sort_key is not None and sort_key <= self.previous_sort_key:
                raise RuntimeError("temporal stream is not strictly ordered by contract and record_key")
            self.previous_sort_key = sort_key
            self.patients.add((str(row["subject_id"]), str(row["hadm_id"])))
        result = validate_value_extract(
            chunk, self.registry, self.allowed, self.expected,
            contract_specs=self.contract_specs, enforce_contract_counts=False)
        if result["held_variables"]:
            raise RuntimeError("temporal value validation produced variable HOLD")
        for contract, values in result["contract_counts"].items():
            for index, value in enumerate(values):
                self.counts[contract][index] += int(value)
        self.exclusions.update(result["exclusion_counts"])
        self.warnings.update(result["warning_counts"])
        self.record_count += int(result["record_count"])

    def result(self) -> dict[str, Any]:
        return {
            "record_count": self.record_count,
            "time_included_count": sum(values[1] for values in self.counts.values()),
            "value_eligible_count": sum(values[2] for values in self.counts.values()),
            "value_exclusion_count": sum(values[1] - values[2] for values in self.counts.values()),
            "contract_counts": self.counts,
            "exclusion_counts": self.exclusions,
            "warning_counts": self.warnings,
            "patient_n": len(self.patients),
            "held_variables": [],
        }


def _source_count_rows(contracts: list[dict[str, str]], validation: dict[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    exclusions = validation["exclusion_counts"]
    for contract in contracts:
        contract_id = contract["source_contract_id"]; actual = validation["contract_counts"][contract_id]
        rows.append({
            "row_type": "CONTRACT_TOTAL", "variable_id": contract["variable_id"],
            "source_contract_id": contract_id, "exclusion_code": "",
            "record_count": str(actual[0]), "time_included_count": str(actual[1]),
            "value_eligible_count": str(actual[2]),
            "value_exclusion_count": str(actual[1] - actual[2]),
            "exclusion_count": str(sum(count for (source, _code), count in exclusions.items()
                                           if source == contract_id)), "status": "PASS",
        })
        for (source, code), count in sorted(exclusions.items()):
            if source != contract_id:
                continue
            rows.append({
                "row_type": "EXCLUSION_CODE", "variable_id": contract["variable_id"],
                "source_contract_id": contract_id, "exclusion_code": code,
                "record_count": "", "time_included_count": "", "value_eligible_count": "",
                "value_exclusion_count": "", "exclusion_count": str(count), "status": "PASS",
            })
    return rows


def _cleanup_stage(root: Path, stage_root: Path | None) -> None:
    if stage_root is None or not stage_root.exists():
        return
    resolved = stage_root.resolve(strict=False)
    resolved.relative_to(root)
    if not resolved.name.startswith(".temporal-materialization-"):
        raise RuntimeError("refusing to remove non-temporal staging directory")
    shutil.rmtree(resolved)


def _stage_path(stage_root: Path, relative: str) -> Path:
    path = (stage_root / relative).resolve(strict=False)
    path.relative_to(stage_root.resolve(strict=False))
    return path


def _publish_artifact_set(root: Path, stage_root: Path, touched_scope: list[str],
                          claim_path: Path, attempt_id: str) -> None:
    ordered = [
        "data/temporal/TEMPORAL_INDEX_OUTCOME.csv",
        "data/temporal/TEMPORAL_C2_RAW.csv",
        "data/temporal/TEMPORAL_EXTENDED_157.csv",
        "audit/temporal/TEMPORAL_RAW_STREAM.readonly.json",
        "audit/temporal/TEMPORAL_SOURCE_COUNTS.csv",
        "audit/temporal/TEMPORAL_AGGREGATION_RECEIPT.json",
        "audit/temporal/TEMPORAL_UNSEAL_LOG.json",
        "audit/temporal/TEMPORAL_MATERIALIZATION_RECEIPT.json",
    ]
    if set(ordered) != ALL_FUTURE_ARTIFACTS:
        raise RuntimeError("temporal transactional artifact set drift")
    _assert_claim_owner(claim_path, attempt_id)
    staged_hashes = {}
    for relative in ordered:
        staged = _stage_path(stage_root, relative)
        if not staged.is_file():
            raise RuntimeError(f"staged temporal artifact missing: {relative}")
        staged_hashes[relative] = _sha256(staged)
    log_relative = "audit/temporal/TEMPORAL_UNSEAL_LOG.json"
    for relative in ordered:
        target = _inside(root, relative, "temporal publish")
        if target.exists() and relative != log_relative:
            raise RuntimeError(f"formal temporal artifact pre-exists: {relative}")
    published: list[str] = []
    try:
        for relative in ordered:
            _assert_claim_owner(claim_path, attempt_id)
            target = _inside(root, relative, "temporal publish"); target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(_stage_path(stage_root, relative), target)
            published.append(relative); touched_scope.append(relative)
        _assert_claim_owner(claim_path, attempt_id)
        for relative, expected in staged_hashes.items():
            if _sha256(_inside(root, relative, "temporal publish verify")) != expected:
                raise RuntimeError(f"published temporal hash mismatch: {relative}")
    except Exception:
        for relative in ALL_FUTURE_ARTIFACTS:
            _inside(root, relative, "temporal rollback").unlink(missing_ok=True)
        raise


def _execute_authorized_materialization(
    root: Path, spec: dict[str, Any], receipt: dict[str, Any], seal_path: Path,
    seal_rows: list[dict[str, str]], sources: dict[str, Path], authorization_receipt: Path,
    anchors: dict[str, str],
) -> dict[str, Any]:
    allowed = {(row["subject_id"], row["hadm_id"]) for row in seal_rows}
    master = guarded_master_rows(sources["frozen_master"], allowed)
    master_rows = master["development_rows"]
    parsed_ids = {(row["subject_id"], row["hadm_id"]) for row in master_rows}
    if parsed_ids != allowed or len(master_rows) != 406:
        raise RuntimeError("authorized master rows do not equal temporal identity seal")
    master_by_id = {(row["subject_id"], row["hadm_id"]): row for row in master_rows}
    ordered_master = [master_by_id[(row["subject_id"], row["hadm_id"])] for row in seal_rows]
    cohort_rows = [{field: row.get(field, "") for field in ("subject_id", "hadm_id", "episode_start", "t0")}
                   for row in ordered_master]

    _, contracts = _read_csv(sources["source_contracts"])
    if len(contracts) != 117 or len({row["source_contract_id"] for row in contracts}) != 117:
        raise RuntimeError("temporal source-contract set must contain 117 unique rows")
    _, registry_rows = _read_csv(sources["working_registry"])
    registry = {row["variable_id"]: row for row in registry_rows}
    _, representation = _read_csv(sources["representation"])
    validator = _BoundedValidation(contracts, registry, allowed)
    representatives = _BoundedRepresentativeRows(representation)

    runner = _load_frozen_runner(sources["readonly_runner"])
    if Path(runner.TEMPLATES["a3"]).resolve(strict=False) != sources["a3_sql"]:
        raise RuntimeError("frozen runner SQL path is not the bound A3 SQL")
    digest = hashlib.sha256(); byte_n = 0; physical_line_n = 0

    def decoded_lines():
        nonlocal byte_n, physical_line_n
        for line in runner.iter_readonly_rows(sources["a3_sql"], cohort_rows, contracts, True):
            if not isinstance(line, bytes):
                raise RuntimeError("read-only runner emitted non-byte stream content")
            digest.update(line); byte_n += len(line); physical_line_n += 1
            yield line.decode("utf-8-sig")

    reader = csv.DictReader(decoded_lines())
    if list(reader.fieldnames or []) != VALUE_FIELDS:
        raise RuntimeError("temporal raw stream schema mismatch")
    chunk: list[dict[str, str]] = []
    for row in reader:
        representatives.add(row); chunk.append(row)
        if len(chunk) == VALIDATION_CHUNK_ROWS:
            validator.validate_chunk(chunk); chunk.clear()
    validator.validate_chunk(chunk)
    if physical_line_n < 1:
        raise RuntimeError("temporal raw stream is empty")
    validation = validator.result()
    extended = build_extended_matrix(
        seal_rows, representatives.representatives(), ordered_master, representation)

    c2 = build_c2_raw(seal_rows, ordered_master, sources["p004_matrix_spec"])
    index_rows = [{field: master_by_id[(row["subject_id"], row["hadm_id"])].get(field, "")
                   for field in INDEX_FIELDS} for row in seal_rows]
    tables = {
        "data/temporal/TEMPORAL_INDEX_OUTCOME.csv": (INDEX_FIELDS, index_rows),
        "data/temporal/TEMPORAL_C2_RAW.csv": (c2["fieldnames"], c2["rows"]),
        "data/temporal/TEMPORAL_EXTENDED_157.csv": (extended["fieldnames"], extended["rows"]),
    }
    schemas = {item["path"]: list(item["columns"]) for item in spec["future_tables"]}
    seal_ids = [(row["subject_id"], row["hadm_id"]) for row in seal_rows]
    if set(tables) != set(schemas):
        raise RuntimeError("temporal table path set drift")
    for name, (fields, rows) in tables.items():
        if fields != schemas[name] or len(rows) != 406:
            raise RuntimeError(f"temporal output schema/count drift: {name}")
        if [(row["subject_id"], row["hadm_id"]) for row in rows] != seal_ids:
            raise RuntimeError(f"temporal output identity order drift: {name}")

    stage_root = Path(tempfile.mkdtemp(prefix=".temporal-materialization-", dir=root)).resolve()
    try:
        for relative, (fields, rows) in tables.items():
            _write_csv(_stage_path(stage_root, relative), fields, rows)
        count_rows = _source_count_rows(contracts, validation)
        count_relative = "audit/temporal/TEMPORAL_SOURCE_COUNTS.csv"
        _write_csv(_stage_path(stage_root, count_relative), list(count_rows[0]), count_rows)
        stream_sha = digest.hexdigest().upper(); seal_sha = _sha256(seal_path)
        raw_receipt = {
            "stage": "temporal", "transaction_read_only": "on", "rollback_declared": True,
            "runner": {"path": sources["readonly_runner"].relative_to(root).as_posix(),
                       "sha256": _sha256(sources["readonly_runner"])},
            "sql": {"path": sources["a3_sql"].relative_to(root).as_posix(),
                    "sha256": _sha256(sources["a3_sql"])},
            "a4_manifest": {"path": anchors["a4_manifest_path"],
                            "sha256": anchors["a4_manifest_sha256"]},
            "temporal_materialization_spec": {
                "path": anchors["temporal_materialization_spec_path"],
                "sha256": anchors["temporal_materialization_spec_sha256"]},
            "temporal_identity_seal": {"path": seal_path.relative_to(root).as_posix(),
                                       "sha256": seal_sha, "identity_n": 406},
            "source_contract_n": 117, "identity_n": 406, "stream_bytes": byte_n,
            "stream_physical_line_n": physical_line_n,
            "stream_data_rows": validation["record_count"],
            "canonical_stream_sha256": stream_sha, "patient_level_raw_file": None,
            "bounded_validation_chunk_rows": VALIDATION_CHUNK_ROWS,
        }
        raw_relative = "audit/temporal/TEMPORAL_RAW_STREAM.readonly.json"
        _write_json(_stage_path(stage_root, raw_relative), raw_receipt)
        table_hashes = {relative: _sha256(_stage_path(stage_root, relative)) for relative in sorted(tables)}
        raw_hash = _sha256(_stage_path(stage_root, raw_relative))
        counts_hash = _sha256(_stage_path(stage_root, count_relative))
        closure_basis = {
            "canonical_stream_sha256": stream_sha,
            "temporal_identity_seal_sha256": seal_sha,
            "aggregation_code_sha256": _sha256(sources["aggregation_code"]),
            "raw_stream_receipt_sha256": raw_hash,
            "source_counts_sha256": counts_hash,
            "table_hashes": table_hashes,
            "record_count": validation["record_count"],
            "time_included_count": validation["time_included_count"],
            "value_eligible_count": validation["value_eligible_count"],
        }
        closure_sha = _json_digest(closure_basis)
        aggregation_receipt = {
            "status": "PASS", "identity_n": 406, "patient_deletion_n": 0,
            "source_contract_n": 117, "record_count": validation["record_count"],
            "time_included_count": validation["time_included_count"],
            "value_eligible_count": validation["value_eligible_count"],
            "value_exclusion_count": validation["value_exclusion_count"],
            "variable_hold_n": 0, "representation_column_n": 157,
            "canonical_stream_sha256": stream_sha, "table_hashes": table_hashes,
            "input_audit_hashes": {raw_relative: raw_hash, count_relative: counts_hash},
            "canonical_stream_to_tables_closure": dict(closure_basis, status="PASS"),
            "closure_sha256": closure_sha,
        }
        aggregation_relative = "audit/temporal/TEMPORAL_AGGREGATION_RECEIPT.json"
        _write_json(_stage_path(stage_root, aggregation_relative), aggregation_receipt)
        audit_hashes = {
            raw_relative: raw_hash,
            count_relative: counts_hash,
            aggregation_relative: _sha256(_stage_path(stage_root, aggregation_relative)),
        }
        return {
            "status": "COMPLETE", "compression_branch": receipt["compression_branch"],
            "authorization_receipt_sha256": _sha256(authorization_receipt),
            "canonical_stream_sha256": stream_sha, "record_count": validation["record_count"],
            "time_included_count": validation["time_included_count"],
            "value_eligible_count": validation["value_eligible_count"],
            "value_exclusion_count": validation["value_exclusion_count"],
            "table_hashes": table_hashes, "audit_hashes": audit_hashes,
            "closure_sha256": closure_sha, "_stage_root": stage_root,
        }
    except Exception:
        _cleanup_stage(root, stage_root)
        raise


def materialize_temporal(project_root, authorization_receipt) -> dict[str, Any]:
    """Execute the one authorized temporal materialization attempt."""
    root = Path(project_root).resolve(strict=False)
    receipt_path = Path(authorization_receipt).resolve(strict=False)
    spec, receipt, seal_path, seal_rows, binding_plan, anchors = _authorization_preflight(root, receipt_path)
    execution = spec["one_time_execution"]
    log_path = _inside(root, str(execution.get("log_path", "")), "temporal unseal log")
    final_path = _inside(root, str(execution.get("receipt_path", "")), "temporal final receipt")
    claim_path = _inside(root, str(execution.get("claim_path", "")), "temporal execution claim")
    started_at = _utc_now(); seal_sha = _sha256(seal_path); attempt_id = uuid.uuid4().hex.upper()
    touched_scope = ["authorization_preflight", "a4_manifest", "temporal_materialization_spec",
                     "temporal_identity_seal", "exclusive_claim", "unseal_log"]
    claim = {
        "claim_status": "RUNNING", "attempt": 1, "attempt_id": attempt_id,
        "operator": str(receipt["operator"]), "pid": os.getpid(), "started_at_utc": started_at,
        "authorization_receipt_sha256": _sha256(receipt_path),
        "a4_manifest_sha256": anchors["a4_manifest_sha256"],
        "temporal_materialization_spec_sha256": anchors["temporal_materialization_spec_sha256"],
    }
    _acquire_claim(claim_path, claim)
    started = {
        "attempt": 1, "attempt_id": attempt_id, "status": "STARTED",
        "operator": str(receipt["operator"]),
        "started_at_utc": started_at, "ended_at_utc": None,
        "authorization_receipt_sha256": _sha256(receipt_path),
        "a4_manifest_sha256": anchors["a4_manifest_sha256"],
        "temporal_materialization_spec_sha256": anchors["temporal_materialization_spec_sha256"],
        "temporal_identity_seal_path": seal_path.relative_to(root).as_posix(),
        "temporal_identity_seal_sha256": seal_sha, "temporal_identity_n": 406,
        "compression_branch": receipt["compression_branch"], "materialization_complete": False,
        "final_receipt_written": False, "table_hashes": {}, "audit_hashes": {},
        "failure_stage": None, "touched_scope": touched_scope,
        "claim_path": claim_path.relative_to(root).as_posix(),
    }
    stage_root: Path | None = None; failure_stage = "start_log"
    try:
        _assert_claim_owner(claim_path, attempt_id)
        _atomic_json(log_path, started)
        failure_stage = "verify_claimed_value_sources"
        _assert_claim_owner(claim_path, attempt_id)
        sources = _verify_claimed_value_sources(root, binding_plan, claim_path, attempt_id)
        touched_scope.append("claimed_value_source_bindings")
        failure_stage = "stream_and_aggregate"
        _assert_claim_owner(claim_path, attempt_id)
        touched_scope.append("database_stream")
        result = _execute_authorized_materialization(
            root, spec, receipt, seal_path, seal_rows, sources, receipt_path, anchors)
        stage_root = result.pop("_stage_root")
        _assert_claim_owner(claim_path, attempt_id)
        touched_scope.extend(["bounded_stream_validation", "frozen_aggregation", "staged_hash_closure"])
        completed_scope = [*touched_scope, *sorted(ALL_FUTURE_ARTIFACTS)]
        completed_log = dict(
            started, status="COMPLETE", ended_at_utc=_utc_now(), materialization_complete=True,
            final_receipt_written=True, canonical_stream_sha256=result["canonical_stream_sha256"],
            closure_sha256=result["closure_sha256"], table_hashes=result["table_hashes"],
            audit_hashes=result["audit_hashes"], failure_stage=None, touched_scope=completed_scope,
        )
        staged_log = _stage_path(stage_root, "audit/temporal/TEMPORAL_UNSEAL_LOG.json")
        _write_json(staged_log, completed_log)
        final = dict(
            result, attempt=1, attempt_id=attempt_id, operator=str(receipt["operator"]),
            started_at_utc=started_at,
            ended_at_utc=completed_log["ended_at_utc"], materialization_complete=True,
            unseal_log_sha256=_sha256(staged_log),
            temporal_materialization_spec_sha256=anchors["temporal_materialization_spec_sha256"],
            a4_manifest_sha256=anchors["a4_manifest_sha256"],
            temporal_identity_seal_sha256=seal_sha,
        )
        _write_json(_stage_path(stage_root,
            "audit/temporal/TEMPORAL_MATERIALIZATION_RECEIPT.json"), final)
        failure_stage = "publish_artifact_set"
        _publish_artifact_set(root, stage_root, touched_scope, claim_path, attempt_id)
        _cleanup_stage(root, stage_root); stage_root = None
        return final
    except Exception as exc:
        # A process that no longer owns the atomic claim may not overwrite the
        # formal failure record or clean another owner's publication state.
        _assert_claim_owner(claim_path, attempt_id)
        _cleanup_stage(root, stage_root)
        for relative in ALL_FUTURE_ARTIFACTS:
            if relative != "audit/temporal/TEMPORAL_UNSEAL_LOG.json":
                _inside(root, relative, "temporal failure cleanup").unlink(missing_ok=True)
        failed = dict(
            started, status="FAILED", ended_at_utc=_utc_now(), materialization_complete=False,
            final_receipt_written=False, failure_stage=failure_stage,
            touched_scope=touched_scope, error_type=type(exc).__name__, error_message=str(exc),
        )
        _atomic_json(log_path, failed)
        final_path.unlink(missing_ok=True)
        raise
    finally:
        _release_claim(claim_path, attempt_id)
