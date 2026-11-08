"""Development preprocessing: A5 code with frozen GCS-M design correction.

See docs/sdd/task-06-report.md for source binding and differences.
"""

from __future__ import annotations

import math
import statistics
import csv
import json
from pathlib import Path
from collections import defaultdict
from typing import Any, Mapping, Sequence


FROZEN_STAGE_ORDER = [
    "coverage",
    "variance_nzv",
    "winsorization",
    "pairwise_complete_redundancy",
    "representative_selection",
    "imputation",
    "scaling",
]

C2_COLUMNS = [
    "age_linear", "age_rcs_nonlinear", "sex_female", "charlson_prior_no_age",
    "log1p_rbc_completed_ml", "non_rbc_component_ever",
    "log1p_vasopressor_active_hours", "log1p_cumulative_invasive_ventilation_hours",
    "icp_monitor_or_csf_diversion_ever", "cranial_operation_ever",
    "log1p_icu_episode_hours", "modified_non_neuro_sofa2_total",
    "abnormal_non_neuro_organ_count", "nonreactive_pupil_count",
    "pupil_unassessable", "gcsm_4_5", "gcsm_1_3", "gcsm_unassessable",
    "log2_wpr_imputed", "wpr_unavailable",
]

C2_BINARY_COLUMNS = {
    "sex_female", "non_rbc_component_ever", "icp_monitor_or_csf_diversion_ever",
    "cranial_operation_ever", "pupil_unassessable", "gcsm_4_5", "gcsm_1_3",
    "gcsm_unassessable", "wpr_unavailable",
}


def _is_binary_value(value: Any) -> bool:
    try:
        return value is not None and value in (0, 1)
    except (TypeError, ValueError):
        return False


def load_frozen_preprocessing_spec(project_root: str | Path) -> dict[str, Any]:
    """Load ordered A4 extension types from the hash-bound field dictionary."""
    from .a5_common import AUTHORITY_HASHES, _project_root, _sha256
    root = _project_root(project_root)
    path = root / "config/A4_FIELD_DICTIONARY.csv"
    if _sha256(path) != AUTHORITY_HASHES["config/A4_FIELD_DICTIONARY.csv"]:
        raise ValueError("A4 field dictionary hash drift")
    with path.open(encoding="utf-8", newline="") as handle:
        rows = [row for row in csv.DictReader(handle) if row["table_name"] == "DEVELOPMENT_EXTENDED_157" and row["role"] != "identity"]
    if len(rows) != 157 or len({row["column_name"] for row in rows}) != 157:
        raise ValueError("A4 extension dictionary width drift")
    frozen = json.loads((root / "config/PREPROCESSING_SPEC.json").read_text(encoding="utf-8"))
    if frozen.get("stage_order") != FROZEN_STAGE_ORDER:
        raise ValueError("preprocessing stage order drift")
    return {**frozen, "project_root": str(root), "field_order": [row["column_name"] for row in rows],
            "binary_columns": [row["column_name"] for row in rows if row["data_type"] == "binary"]}


def validate_fit_partition(role: str) -> dict[str, str]:
    forbidden = {"outer_test", "inner_validation", "temporal", "full_development_before_final_refit"}
    if role in forbidden:
        raise PermissionError(f"preprocessing cannot fit on {role}")
    if role not in {"inner_training", "outer_training_refit", "final_full_development_refit"}:
        raise ValueError("unknown preprocessing partition role")
    return {"status": "PASS"}


def _quantile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        raise ValueError("quantile requires an observed value")
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _rcs_one(value: float, knots: Sequence[float]) -> float:
    k1, k2, k3 = map(float, knots)
    if not k1 < k2 < k3:
        return 0.0
    positive = lambda number: max(number, 0.0) ** 3
    return (positive(value - k1)
            - positive(value - k2) * (k3 - k1) / (k3 - k2)
            + positive(value - k3) * (k2 - k1) / (k3 - k2)) / (k3 - k1) ** 2


def generate_c2_design(rows: Sequence[Mapping[str, Any]], train_index: Sequence[int]):
    if not rows or not train_index:
        raise ValueError("C2 design requires rows and training indices")
    required = {"gender", "charlson_prior_no_age", "rbc_completed_ml", "non_rbc_component_ever",
                "vasopressor_active_hours", "cumulative_invasive_ventilation_hours",
                "icp_monitor_or_csf_diversion_ever", "cranial_operation_ever", "icu_episode_hours",
                "modified_non_neuro_sofa2_total", "abnormal_non_neuro_organ_count",
                "nonreactive_pupil_count", "pupil_unassessable", "wpr_unavailable"}
    for row in rows:
        if any(key not in row or row[key] is None for key in required) or \
                not any(row.get(k) is not None for k in ("age_at_admit", "age")) or \
                not any(row.get(k) is not None for k in ("gcsm_category", "gcsm")) or \
                not any(k in row for k in ("log2_wpr", "wpr")):
            raise ValueError("CORE_DATA_FAILURE: required raw core value missing")
        if row.get("log2_wpr") is None and row.get("wpr") is None and row["wpr_unavailable"] != 1:
            raise ValueError("CORE_DATA_FAILURE: WPR availability mismatch")
        if str(row["gender"]).upper() not in {"F", "M"} or \
                str(row.get("gcsm_category", row.get("gcsm"))) not in {"1-3", "4-5", "6", "unassessable"}:
            raise ValueError("CORE_DATA_FAILURE: invalid core category")
    ages = [float(rows[index].get("age_at_admit", rows[index].get("age"))) for index in train_index]
    knots = [_quantile(ages, probability) for probability in (0.10, 0.50, 0.90)]
    observed_wpr: list[float] = []
    for index in train_index:
        row = rows[index]
        raw = row.get("log2_wpr")
        if raw is None:
            ratio = row.get("wpr")
            raw = None if ratio is None else math.log2(float(ratio))
        if raw is not None and math.isfinite(float(raw)):
            observed_wpr.append(float(raw))
    if not observed_wpr:
        raise ValueError("WPR training median is not estimable")
    wpr_median = float(statistics.median(observed_wpr))

    matrix: list[list[float]] = []
    for row in rows:
        age = float(row.get("age_at_admit", row.get("age")))
        gender = str(row["gender"]).upper()
        gcsm = str(row.get("gcsm_category", row.get("gcsm")))
        raw_wpr = row.get("log2_wpr")
        if raw_wpr is None:
            ratio = row.get("wpr")
            raw_wpr = None if ratio is None else math.log2(float(ratio))
        log2_wpr = wpr_median if raw_wpr is None else float(raw_wpr)
        unavailable = int(row["wpr_unavailable"])
        vector = [
            age,
            _rcs_one(age, knots),
            float(gender == "F"),
            float(row["charlson_prior_no_age"]),
            math.log1p(float(row["rbc_completed_ml"])),
            float(row["non_rbc_component_ever"]),
            math.log1p(float(row["vasopressor_active_hours"])),
            math.log1p(float(row["cumulative_invasive_ventilation_hours"])),
            float(row["icp_monitor_or_csf_diversion_ever"]),
            float(row["cranial_operation_ever"]),
            math.log1p(float(row["icu_episode_hours"])),
            float(row["modified_non_neuro_sofa2_total"]),
            float(row["abnormal_non_neuro_organ_count"]),
            float(row["nonreactive_pupil_count"]),
            float(row["pupil_unassessable"]),
            float(gcsm == "4-5"),
            float(gcsm == "1-3"),
            float(gcsm == "unassessable"),
            log2_wpr,
            float(unavailable),
        ]
        if len(vector) != 20 or not all(math.isfinite(value) for value in vector):
            raise AssertionError("C2 design width drift")
        matrix.append(vector)
    return matrix, list(C2_COLUMNS), {"age_knots": knots, "wpr_median": wpr_median}


def maximum_m2_width(core_width: int, extension_width: int) -> int:
    if core_width != 20 or extension_width != 157:
        raise ValueError("Task05 frozen dimensions are 20+157")
    return 177


def _unique_observed(rows: Sequence[Mapping[str, Any]], indices: Sequence[int], column: str) -> list[Any]:
    return [rows[index].get(column) for index in indices if rows[index].get(column) is not None]


def _near_zero_variance(values: Sequence[Any]) -> bool:
    unique = set(values)
    if len(unique) <= 1:
        return True
    percent_unique = 100.0 * len(unique) / len(values)
    counts = sorted((values.count(item) for item in unique), reverse=True)
    ratio = counts[0] / counts[1]
    return percent_unique <= 10.0 and ratio > 19.0


def _equivalent(values_a: Sequence[Any], values_b: Sequence[Any]) -> bool:
    paired = [(a, b) for a, b in zip(values_a, values_b) if a is not None and b is not None]
    if not paired:
        return False
    if all(float(a) == float(b) for a, b in paired):
        return True
    unique = {float(value) for pair in paired for value in pair}
    return unique <= {0.0, 1.0} and all(float(a) == 1.0 - float(b) for a, b in paired)


def fit_preprocessor(core_raw: Sequence[Mapping[str, Any]], extension: Sequence[Mapping[str, Any]],
                     train_index: Sequence[int], spec: Mapping[str, Any], model_family: str) -> dict[str, Any]:
    validate_fit_partition(spec.get("partition_role"))
    if spec.get("source_role", spec["partition_role"]) != spec["partition_role"]:
        raise PermissionError("preprocessing source and partition roles differ")
    if model_family.upper() not in {"EN", "RSF", "GBST"}:
        raise ValueError("unknown preprocessing model family")
    frozen = load_frozen_preprocessing_spec(spec["project_root"])
    if spec.get("field_order") != frozen["field_order"] or spec.get("binary_columns") != frozen["binary_columns"]:
        raise ValueError("frozen extension metadata drift")
    if spec.get("model_level") == "M1" and extension is not None:
        raise PermissionError("M1 cannot open the extension table")
    if extension is None and spec.get("model_level") == "M1":
        extension = [{} for _ in core_raw]
        field_order = []
        binary_columns = set()
    else:
        field_order = frozen["field_order"]
        binary_columns = set(frozen["binary_columns"])
    if len(core_raw) != len(extension):
        raise ValueError("core and extension identity spines differ")
    if any(set(row) != set(field_order) for row in extension):
        raise ValueError("extension field schema drift")
    stage_order = list(spec.get("stage_order", FROZEN_STAGE_ORDER))
    if stage_order != FROZEN_STAGE_ORDER:
        raise ValueError("preprocessing stage order drift")
    if not train_index or len(set(train_index)) != len(train_index) or min(train_index) < 0 or max(train_index) >= len(core_raw):
        raise ValueError("invalid training indices")
    core_columns = list(C2_COLUMNS)
    if not core_raw or any(list(row) != core_columns for row in core_raw) or any(core_raw[index].get(column) is None or not math.isfinite(float(core_raw[index][column])) for index in train_index for column in core_columns):
        raise ValueError("CORE_DATA_FAILURE")
    core_means = {}
    core_scales = {}
    for column in core_columns:
        values = [float(core_raw[index][column]) for index in train_index]
        mean = sum(values) / len(values)
        sd = math.sqrt(sum((value - mean) ** 2 for value in values) / len(values))
        if not math.isfinite(sd) or sd <= 0:
            raise ValueError("CORE_DEGENERACY")
        core_means[column] = mean
        core_scales[column] = sd
    for column in binary_columns:
        for row in extension:
            value = row.get(column)
            if not _is_binary_value(value):
                raise ValueError("BINARY_CONTRACT_FAILURE")

    extension_columns = field_order
    inactive: dict[str, str] = {}
    coverage: dict[str, int] = {}
    threshold = max(50, math.ceil(0.05 * len(train_index)))
    survivors: list[str] = []
    for column in extension_columns:
        values = _unique_observed(extension, train_index, column)
        coverage[column] = len(values)
        if column not in binary_columns and len(values) < threshold:
            inactive[column] = "LOW_COVERAGE"
        elif _near_zero_variance(values):
            inactive[column] = "ZERO_OR_NEAR_ZERO_VARIANCE"
        else:
            survivors.append(column)

    winsor: dict[str, tuple[float, float]] = {}
    for column in survivors:
        if column in binary_columns:
            continue
        values = [float(value) for value in _unique_observed(extension, train_index, column)]
        winsor[column] = (_quantile(values, 0.01), _quantile(values, 0.99))

    # Protected C2 columns are vertices in the correlation graph even though
    # none may be dropped. They can connect otherwise unrelated extensions.
    graph_columns = core_columns + survivors if survivors else []
    parent = {column: column for column in graph_columns}

    def find(column: str) -> str:
        while parent[column] != column:
            parent[column] = parent[parent[column]]
            column = parent[column]
        return column

    def union(left: str, right: str) -> None:
        a, b = find(left), find(right)
        if a != b:
            parent[b] = a

    def observed(column):
        source = core_raw if column in core_columns else extension
        return [source[index].get(column) for index in train_index]

    graph_binary = binary_columns | C2_BINARY_COLUMNS
    for left_position, left in enumerate(graph_columns):
        left_values = observed(left)
        for right in graph_columns[left_position + 1:]:
            right_values = observed(right)
            overlap = sum(a is not None and b is not None for a, b in zip(left_values, right_values))
            if overlap < threshold:
                continue
            if _equivalent(left_values, right_values):
                union(left, right)
                continue
            paired = [(float(a), float(b)) for a, b in zip(left_values, right_values)
                      if a is not None and b is not None]
            left_complete = [pair[0] for pair in paired]
            right_complete = [pair[1] for pair in paired]
            if left in winsor:
                low, high = winsor[left]
                left_complete = [min(max(value, low), high) for value in left_complete]
            if right in winsor:
                low, high = winsor[right]
                right_complete = [min(max(value, low), high) for value in right_complete]
            from scipy.stats import pearsonr, spearmanr
            if left in graph_binary and right in graph_binary:
                coefficient = float(pearsonr(left_complete, right_complete).statistic)
            else:
                coefficient = float(spearmanr(left_complete, right_complete).statistic)
            if math.isfinite(coefficient) and abs(coefficient) >= 0.95:
                union(left, right)

    components: dict[str, list[str]] = defaultdict(list)
    for column in graph_columns:
        components[find(column)].append(column)
    declared_order = {column: position for position, column in enumerate(extension_columns)}
    graph_order = {column: position for position, column in enumerate(graph_columns)}
    representatives: dict[str, str] = {}
    correlation_components: list[list[str]] = []
    retained: list[str] = []
    for members in components.values():
        extensions = [column for column in members if column in declared_order]
        if not extensions:
            continue
        representative = sorted(extensions, key=lambda column: (-coverage[column], declared_order[column], column))[0]
        retained.append(representative)
        if len(members) > 1:
            correlation_components.append(sorted(members, key=lambda column: graph_order[column]))
            key = "|".join(sorted(extensions, key=lambda column: declared_order[column]))
            representatives[key] = representative
            for column in extensions:
                if column != representative:
                    inactive[column] = f"REDUNDANT_WITH:{representative}"
    retained.sort(key=lambda column: declared_order[column])

    medians: dict[str, float] = {}
    means: dict[str, float] = {}
    scales: dict[str, float] = {}
    final_extension: list[str] = []
    for column in retained:
        values = [float(value) for value in _unique_observed(extension, train_index, column)]
        median = float(statistics.median(values))
        medians[column] = median
        prepared: list[float] = []
        for index in train_index:
            value = extension[index].get(column)
            numeric = median if value is None else float(value)
            if column in winsor:
                numeric = min(max(numeric, winsor[column][0]), winsor[column][1])
            prepared.append(numeric)
        mean = sum(prepared) / len(prepared)
        variance = sum((value - mean) ** 2 for value in prepared) / len(prepared)
        sd = math.sqrt(variance)
        if sd == 0.0:
            inactive[column] = "POST_IMPUTATION_SD0"
            continue
        means[column] = mean
        scales[column] = sd if model_family.upper() == "EN" else 1.0
        final_extension.append(column)

    return {
        "stage_order": stage_order,
        "training_n": len(train_index),
        "training_indices": list(train_index),
        "target_rows_used_for_fit": 0,
        "core_columns": core_columns,
        "core_means": core_means,
        "core_scales": core_scales,
        "extension_columns": final_extension,
        "inactive_columns": inactive,
        "coverage": coverage,
        "winsor_limits": winsor,
        "representatives": representatives,
        "correlation_components": correlation_components,
        "medians": medians,
        "means": means,
        "scales": scales,
        "binary_columns": sorted(binary_columns),
        "model_family": model_family.upper(),
        "final_columns": core_columns + final_extension,
    }


def transform_preprocessor(preprocessor: Mapping[str, Any], core_raw: Sequence[Mapping[str, Any]],
                           extension: Sequence[Mapping[str, Any]], target_index: Sequence[int]):
    rows: list[list[float]] = []
    binary = set(preprocessor.get("binary_columns", []))
    # Validate every frozen binary target field before constructing any row,
    # including fields filtered out during training. M1 records no extension
    # binary fields, so this branch never opens its extension input.
    if binary:
        for index in target_index:
            if extension is None or not isinstance(extension[index], Mapping):
                raise ValueError("BINARY_CONTRACT_FAILURE")
            if any(not _is_binary_value(extension[index].get(column)) for column in binary):
                raise ValueError("BINARY_CONTRACT_FAILURE")
    for index in target_index:
        vector: list[float] = []
        for column in preprocessor["core_columns"]:
            value = core_raw[index].get(column)
            if value is None:
                raise ValueError("CORE_DATA_FAILURE")
            numeric = float(value)
            if not math.isfinite(numeric):
                raise ValueError("CORE_DATA_FAILURE")
            if preprocessor["model_family"] == "EN":
                numeric = (numeric - preprocessor["core_means"][column]) / preprocessor["core_scales"][column]
            vector.append(numeric)
        for column in preprocessor["extension_columns"]:
            value = extension[index].get(column)
            numeric = preprocessor["medians"][column] if value is None else float(value)
            if column in preprocessor["winsor_limits"]:
                low, high = preprocessor["winsor_limits"][column]
                numeric = min(max(numeric, low), high)
            if preprocessor["model_family"] == "EN":
                numeric = (numeric - preprocessor["means"][column]) / preprocessor["scales"][column]
            vector.append(numeric)
        rows.append(vector)
    return rows

