"""Development-only descriptive profiling for Task 04."""

from __future__ import annotations

import math
import statistics
from collections import Counter
from typing import Any, Iterable, Mapping


DEVELOPMENT_ERAS = ("2008 - 2010", "2011 - 2013", "2014 - 2016", "2017 - 2019")
FORBIDDEN_FIELDS = {
    "death_90d", "death_180d", "death_days_after_t0", "outcome", "label", "prediction",
    "deathtime", "dod", "anchor_year_group",
}


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _quantile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    low = int(math.floor(position)); high = int(math.ceil(position))
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _profile(column: str, raw: list[Any], data_type: str) -> dict[str, Any]:
    present = [value for value in raw if value not in (None, "")]
    counts = Counter(str(value) for value in present)
    unique_n = len(counts)
    ordered_counts = sorted(counts.values(), reverse=True)
    mode_ratio = (ordered_counts[0] / len(present)) if present else None
    frequency_ratio = (ordered_counts[0] / ordered_counts[1]) if len(ordered_counts) > 1 else math.inf
    percent_unique = 100.0 * unique_n / len(present) if present else 0.0
    numbers = [_number(value) for value in present]
    numeric = [value for value in numbers if value is not None]
    q1, median, q3 = _quantile(numeric, 0.25), _quantile(numeric, 0.5), _quantile(numeric, 0.75)
    return {
        "column_name": column, "data_type": data_type, "n": len(raw), "nonmissing_n": len(present),
        "coverage": len(present) / len(raw) if raw else 0.0, "missing_n": len(raw) - len(present),
        "unique_valid_n": unique_n, "percent_unique": percent_unique,
        "most_common_ratio": mode_ratio, "frequency_ratio": frequency_ratio,
        "minimum": min(numeric) if numeric else None, "maximum": max(numeric) if numeric else None,
        "mean": statistics.fmean(numeric) if numeric else None,
        "sd": statistics.stdev(numeric) if len(numeric) > 1 else (0.0 if len(numeric) == 1 else None),
        "median": median, "q1": q1, "q3": q3, "iqr": (q3 - q1) if q1 is not None else None,
        "all_missing_flag": "1" if not present else "0",
        "zero_variance_flag": "1" if unique_n <= 1 else "0",
        "near_zero_variance_descriptive_flag": "1" if present and percent_unique <= 10 and frequency_ratio > 19 else "0",
        "status": "DESCRIPTIVE_ONLY",
    }


def profile_development_columns(matrix: Iterable[Mapping[str, Any]],
                                development_era: Mapping[tuple[str, str], str],
                                column_types: Mapping[str, str]) -> dict[str, Any]:
    rows = [dict(row) for row in matrix]
    for row in rows:
        forbidden = FORBIDDEN_FIELDS.intersection(row)
        if forbidden:
            raise ValueError(f"outcome/era field cannot enter predictor profiling: {sorted(forbidden)}")
    identities = [(str(row["subject_id"]), str(row["hadm_id"])) for row in rows]
    for identity in identities:
        era = development_era.get(identity)
        if era not in DEVELOPMENT_ERAS:
            raise PermissionError("nondevelopment identity or era rejected before value profiling")
    column_profile = [_profile(column, [row.get(column, "") for row in rows], data_type)
                      for column, data_type in column_types.items()]
    era_rows: list[dict[str, Any]] = []
    for column, data_type in column_types.items():
        overall = next(item for item in column_profile if item["column_name"] == column)
        for era in DEVELOPMENT_ERAS:
            values = [row.get(column, "") for row, identity in zip(rows, identities) if development_era[identity] == era]
            profile = _profile(column, values, data_type)
            era_rows.append({
                "column_name": column, "data_type": data_type, "development_era": era,
                "n": profile["n"], "nonmissing_n": profile["nonmissing_n"], "coverage": profile["coverage"],
                "median": profile["median"] if data_type == "numeric" else None,
                "q1": profile["q1"] if data_type == "numeric" else None,
                "q3": profile["q3"] if data_type == "numeric" else None,
                "one_proportion": (sum(_number(v) == 1 for v in values if v not in (None, "")) /
                                   profile["nonmissing_n"] if data_type == "binary" and profile["nonmissing_n"] else None),
                "overall_coverage": overall["coverage"], "status": "DESCRIPTIVE_ONLY",
            })
    return {"column_profile": column_profile, "era_stability": era_rows,
            "patient_n": len(rows), "status": "DESCRIPTIVE_ONLY"}


def audit_deterministic_redundancy(core_raw: Iterable[Mapping[str, Any]],
                                   extension_matrix: Iterable[Mapping[str, Any]],
                                   field_dictionary: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    core = [dict(row) for row in core_raw]
    extension = [dict(row) for row in extension_matrix]
    if any(FORBIDDEN_FIELDS.intersection(row) for row in core + extension):
        raise ValueError("outcome fields cannot enter redundancy audit")
    columns = [str(row["column_name"]) for row in field_dictionary
               if str(row.get("table_name")) == "DEVELOPMENT_EXTENDED_157" and
               str(row.get("role", "")).startswith("extension_")]
    relations: list[dict[str, str]] = []
    for left_index, left in enumerate(columns):
        left_values = [row.get(left, "") for row in extension]
        for right in columns[left_index + 1:]:
            right_values = [row.get(right, "") for row in extension]
            if left_values == right_values:
                relations.append({"left_column": left, "right_column": right,
                                  "relation": "EXACT_EQUAL", "status": "DESCRIPTIVE_ONLY"})
            elif all(a in {"0", "1"} and b in {"0", "1"} and int(a) + int(b) == 1
                     for a, b in zip(left_values, right_values)):
                relations.append({"left_column": left, "right_column": right,
                                  "relation": "BINARY_COMPLEMENT", "status": "DESCRIPTIVE_ONLY"})
    return {"relations": relations, "retained_columns": columns, "deleted_columns": [],
            "status": "DESCRIPTIVE_ONLY"}
