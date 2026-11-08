"""Task 04 A3 value extraction and deterministic representation controls.

The module is deliberately outcome-agnostic except for the separately built
development index/outcome table.  Master access is identity-first so temporal
rows are never parsed beyond their two identity fields.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping


VALUE_FIELDS = [
    "record_key", "variable_id", "source_contract_id", "source_schema", "source_table", "itemid",
    "subject_id", "hadm_id", "episode_start", "t0", "event_time", "storetime",
    "availability_time", "endtime", "source_unit", "raw_numeric_value", "raw_text_value",
    "rate", "amount", "status_text", "warning_code", "time_included", "included", "exclusion_code",
]
C2_RAW_FIELDS = [
    "age_at_admit", "gender", "charlson_prior_no_age", "rbc_completed_ml",
    "non_rbc_component_ever", "vasopressor_active_hours", "cumulative_invasive_ventilation_hours",
    "icp_monitor_or_csf_diversion_ever", "cranial_operation_ever", "icu_episode_hours",
    "modified_non_neuro_sofa2_total", "abnormal_non_neuro_organ_count",
    "nonreactive_pupil_count", "pupil_unassessable", "gcsm_category", "log2_wpr", "wpr_unavailable",
]
MASTER_EXTENSION_IDS = {
    "hours_since_icp_procedure", "hours_since_cranial_operation", "gcsm_freshness_hours",
    "pupil_freshness_hours", "wpr_freshness_hours", "sofa_rollback_steps",
    "sofa_observed_domain_n", "sofa_all_domains_unobserved_48h",
}
TASK03_HASHES = {
    "inputs/frozen/01_FORMAL_PREFIT_MASTER_N3245.csv": "22416CB01DED6B0C79E473C932B5AAE9BF491C28D6F62D32E19FF6BAC2B7456F",
    "config/A2_DEVELOPMENT_ID_ALLOWLIST.csv": "96C6739AD018B8BD56D1B221353ACD769E494B9D4C3B18D8E221385CD1B3D668",
    "config/EXTENDED_VARIABLE_REGISTRY_A1_FROZEN.csv": "A8ED017D941C7EDD58A3C2F14926F278CAB80CEB9F4EEBB0773BCDAE861904A9",
    "config/EXTENDED_VARIABLE_REGISTRY.csv": "F19ADFCC98A2888C75D05B3CACC476966CFF677D968F31ED85ADDBF0248F0E7E",
    "config/EXTENDED_REPRESENTATION_SPEC.csv": "D8B9E30BCF5FF581900179CA74F0A73F531576F1B6E8D3338CA695A32EABA50A",
    "config/C2_CORE_SEMANTIC_GUARD.csv": "FD7E5CE62EFB2C92F641EBE9D9FDFEA2B818C5B53D4CCEC5C252333A0E9F15AA",
    "audit/a2/A2_RAW_TIME_AUDIT.csv": "7BE2A9BC6C950384BEBBD3B7090EE66EDB50710CF1EAC13FFBF6D4FF6AF1C3BC",
    "audit/a2/A2_SOURCE_SUMMARY.csv": "796340A41B9A674C785F3213E04095E731BC8FDA876B521E1A099E521705165B",
    "inputs/frozen/07_P002_DESIGN_DICTIONARY.csv": "7E28C1E2FFC11E4FC1D7DBEF6364E1FB0C7444E9656DBA224ED06324266C5581",
    "inputs/frozen/08_P004_MATRIX_SPEC.csv": "24D85AC7539D8B41BECE5846FD3A5A0A59EF8B51A73322BB88E0EC8C0145F282",
}
TECHNICAL_TO_PROTOCOL = {
    "initial_neuro": "initial_tbi_neurologic_status",
    "procedure_recency": "neurosurgical_procedures",
    "treatment_exposure": "icu_treatment_intensity",
    "treatment_state": "icu_treatment_intensity",
    "physiology_trajectory": "physiology_trajectory",
    "laboratory_trajectory": "laboratory_trajectory",
    "support_exposure": "organ_support_status",
    "support_recency": "organ_support_status",
    "support_state": "organ_support_status",
    "core_freshness": "clinically_justified_unavailability_freshness",
    "sofa_metadata": "clinically_justified_unavailability_freshness",
}
EXPECTED_DOMAIN_COUNTS = {
    "initial_neuro": (3, 10), "procedure_recency": (2, 4), "treatment_exposure": (1, 1),
    "treatment_state": (2, 2), "physiology_trajectory": (8, 48),
    "laboratory_trajectory": (13, 78), "support_exposure": (1, 1),
    "support_recency": (2, 4), "support_state": (3, 3), "core_freshness": (3, 3),
    "sofa_metadata": (3, 3),
}
SOURCE_KEY_FIELDS = {
    "mimiciv_icu.chartevents": (
        "subject_id", "hadm_id", "stay_id", "itemid", "charttime", "storetime", "caregiver_id",
        "value", "valuenum", "valueuom", "warning",
    ),
    "mimiciv_icu.inputevents": (
        "subject_id", "hadm_id", "stay_id", "itemid", "starttime", "endtime", "storetime",
        "caregiver_id", "orderid", "linkorderid", "amount", "amountuom", "rate", "rateuom",
        "statusdescription", "originalamount", "originalrate",
    ),
    "mimiciv_icu.procedureevents": (
        "subject_id", "hadm_id", "stay_id", "itemid", "starttime", "endtime", "storetime",
        "caregiver_id", "orderid", "linkorderid", "value", "valueuom", "statusdescription",
    ),
}


def _sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest().upper()


def _read_csv(path: str | Path) -> tuple[list[str], list[dict[str, str]]]:
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), list(reader)


def _dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _truth(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "t", "true", "yes"}


def initial_gcs_assessable(status_text: Any) -> bool:
    """Apply the hash-frozen NMB/sedative-plus-RASS interference rule."""
    parts: dict[str, str] = {}
    for token in str(status_text or "").replace(";", "|").split("|"):
        key, separator, value = token.partition("=")
        if separator:
            parts[key.strip()] = value.strip()
    if not {"nmb_active", "sedative_active", "rass"} <= set(parts):
        return False
    if parts["nmb_active"] == "1":
        return False
    try:
        deep_sedation = float(parts["rass"]) <= -4
    except ValueError:
        deep_sedation = False
    return not (parts["sedative_active"] == "1" and deep_sedation)


def validate_task03_authorities(project_root: str | Path) -> dict[str, Any]:
    root = Path(project_root).resolve(strict=False)
    if root.name != "Project_B" or any(part.casefold() == "project_a" for part in root.parts):
        raise PermissionError("canonical Project_B required")
    for relative, expected in TASK03_HASHES.items():
        if _sha256(root / relative) != expected:
            raise ValueError(f"Task03 authority drift: {relative}")
    _, allow = _read_csv(root / "config" / "A2_DEVELOPMENT_ID_ALLOWLIST.csv")
    ids = {(row["subject_id"], row["hadm_id"]) for row in allow}
    if len(allow) != 2839 or len(ids) != 2839:
        raise ValueError("development allowlist identity drift")
    _, summary = _read_csv(root / "audit" / "a2" / "A2_SOURCE_SUMMARY.csv")
    database = [row for row in summary if row["source_table"] != "frozen_master"]
    record_n = sum(int(row["record_count"]) for row in database)
    included_n = sum(int(row["included_count"]) for row in database)
    if len(database) != 117 or (record_n, included_n) != (2802545, 1972182):
        raise ValueError("A2 contract-count authority drift")
    state = json.loads((root / "manifests" / "PROJECT_STATE.json").read_text(encoding="utf-8"))
    if state.get("phase") != "A3_A4" or state.get("fit_authorized") is not False or state.get("temporal_unsealed") is not False:
        raise ValueError("Task04 start-state mismatch")
    return {"status": "PASS", "development_n": 2839, "database_contract_n": 117,
            "record_count": record_n, "included_count": included_n}


_TIME_KEY_FIELDS = {"charttime", "storetime", "starttime", "endtime"}
_DECIMAL_KEY_FIELDS = {"valuenum", "amount", "rate", "originalamount", "originalrate"}
_INTEGER_KEY_FIELDS = {
    "subject_id", "hadm_id", "stay_id", "itemid", "caregiver_id", "orderid", "linkorderid", "warning",
}


def _canonical_timestamp(value: Any) -> str:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is not None:
        parsed = parsed.replace(tzinfo=None)
    return parsed.strftime("%Y-%m-%dT%H:%M:%S.%f")


def _canonical_decimal(value: Any) -> str:
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("non-finite numeric source-key field")
    if number == 0:
        return "0"
    text = format(number, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _canonical_field(field: str, value: Any, *, source_table: str) -> str:
    if value is None:
        return r"\N"
    if field in _TIME_KEY_FIELDS:
        return _canonical_timestamp(value)
    decimal_field = field in _DECIMAL_KEY_FIELDS or (
        source_table == "mimiciv_icu.procedureevents" and field == "value")
    if decimal_field and value != "":
        try:
            return _canonical_decimal(value)
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"invalid numeric source-key field: {field}") from exc
    if field in _INTEGER_KEY_FIELDS and value != "":
        try:
            number = Decimal(str(value))
            if not number.is_finite() or number != number.to_integral_value():
                raise ValueError
            return str(int(number))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"invalid integer source-key field: {field}") from exc
    return str(value)


def canonical_record_keys(rows: Iterable[Mapping[str, Any]], source_table: str,
                          source_contract_id: str) -> list[str]:
    records = [dict(row) for row in rows]
    for row in records:
        if "ctid" in row or "_csv_row_number" in row:
            raise ValueError("physical location or CSV order cannot define a record key")
    if source_table == "mimiciv_hosp.labevents":
        keys = [str(row.get("labevent_id", "")) for row in records]
        if any(not key for key in keys) or len(keys) != len(set(keys)):
            raise ValueError("labevent_id must be nonempty and unique")
        return keys
    fields = SOURCE_KEY_FIELDS.get(source_table)
    if not fields:
        raise ValueError(f"unsupported source table: {source_table}")
    payloads = [chr(31).join(_canonical_field(field, row.get(field), source_table=source_table)
                             for field in fields) for row in records]
    occurrence: Counter[str] = Counter()
    keys: list[str] = []
    for payload in sorted(payloads):
        occurrence[payload] += 1
        serialized = chr(31).join((source_contract_id, payload, str(occurrence[payload])))
        keys.append(hashlib.sha256(serialized.encode("utf-8")).hexdigest().upper())
    if any(not key for key in keys) or len(keys) != len(set(keys)):
        raise ValueError("canonical record keys are not unique")
    return keys


def validate_value_extract(rows: Iterable[Mapping[str, Any]],
                           registry: Mapping[str, Mapping[str, Any]] | Iterable[Mapping[str, Any]],
                           allowlist: set[tuple[str, str]],
                           a2_contract_counts: Mapping[str, Mapping[str, Any]],
                           *, contract_specs: Mapping[str, Mapping[str, Any]] | None = None,
                           enforce_contract_counts: bool = True) -> dict[str, Any]:
    registry_by_id = dict(registry) if isinstance(registry, Mapping) else {
        str(row["variable_id"]): row for row in registry
    }
    observed = {contract: [0, 0, 0] for contract in a2_contract_counts}
    keys: dict[str, set[str]] = defaultdict(set)
    held: set[str] = set()
    hold_reasons: dict[str, set[str]] = defaultdict(set)
    exclusions: Counter[tuple[str, str]] = Counter()
    warnings: Counter[tuple[str, str]] = Counter()
    value_exclusion_patients: set[tuple[str, str]] = set()
    value_exclusion_itemids: Counter[str] = Counter()
    value_exclusion_codes: Counter[str] = Counter()
    patients: set[tuple[str, str]] = set()
    row_n = 0
    for source in rows:
        row = dict(source)
        if not set(VALUE_FIELDS).issubset(row):
            raise ValueError("A3 value-source schema missing clinical value fields")
        identity = (str(row["subject_id"]), str(row["hadm_id"]))
        if identity not in allowlist:
            raise PermissionError("nonallowlist identity rejected before value access")
        patients.add(identity)
        variable = str(row["variable_id"]); contract = str(row["source_contract_id"])
        if variable not in registry_by_id or contract not in observed:
            raise ValueError("unregistered variable or source contract")
        key = str(row["record_key"])
        if not key or key in keys[contract]:
            raise ValueError("record_key is empty or duplicated within source contract")
        keys[contract].add(key); observed[contract][0] += 1; row_n += 1
        def strict_flag(field: str) -> bool:
            value = row.get(field)
            if value is True or value == "t":
                return True
            if value is False or value == "f":
                return False
            held.add(variable); hold_reasons[variable].add("INVALID_ELIGIBILITY_FLAG")
            return False

        time_included = strict_flag("time_included")
        included = strict_flag("included")
        observed[contract][1] += int(time_included)
        observed[contract][2] += int(included)
        event, availability, episode_start, t0 = map(_dt, (
            row["event_time"], row["availability_time"], row["episode_start"], row["t0"],
        ))
        code = str(row.get("exclusion_code", ""))
        warning = str(row.get("warning_code", ""))
        if not included:
            exclusions[(contract, code)] += 1
            if time_included:
                value_exclusion_patients.add(identity)
                value_exclusion_itemids[str(row.get("itemid", ""))] += 1
                value_exclusion_codes[code] += 1
            if code in {"", "OK"}:
                held.add(variable); hold_reasons[variable].add("EXCLUDED_WITH_OK_CODE")
            if warning:
                held.add(variable); hold_reasons[variable].add("WARNING_ON_EXCLUDED_VALUE")
            continue
        if not time_included:
            held.add(variable); hold_reasons[variable].add("VALUE_ELIGIBLE_WITHOUT_TIME_ELIGIBILITY")
        invalid = (
            event is None or availability is None or episode_start is None or t0 is None
            or event < episode_start or event >= t0 or availability > t0 or availability < event
        )
        if code != "OK":
            invalid = True
        if warning:
            if warning != "FUTURE_END_PRESENT_NOT_USED":
                invalid = True
            else:
                warnings[(contract, warning)] += 1
        spec = (contract_specs or {}).get(contract)
        if spec:
            rule = str(spec.get("unit_rule", ""))
            number = _float(row.get("raw_numeric_value"))
            text_value = str(row.get("raw_text_value") or "").strip()
            source_unit = str(row.get("source_unit") or "")
            rate = _float(row.get("rate")); amount = _float(row.get("amount"))
            if rule in {"categorical", "pupil_category"}:
                unit_valid = bool(text_value)
            elif rule in {"itemid_score", "itemid223835_percent"}:
                unit_valid = number is not None
            elif rule == "temperature_c":
                unit_valid = source_unit in {"°C", "Deg. C", "C"}
            elif rule == "temperature_f":
                unit_valid = source_unit in {"°F", "Deg. F", "F"}
            elif rule == "percent":
                unit_valid = source_unit in {"%", ""}
            elif rule == "binary_activity":
                unit_valid = (number is not None and number > 0) or bool(text_value)
            elif rule == "positive_rate":
                unit_valid = (str(spec.get("contract_role", "")) == "completed_stop"
                              or (rate is not None and rate > 0))
            elif rule == "completed_positive_amount":
                unit_valid = amount is not None and amount > 0
            elif rule == "procedure_state":
                # The procedure contract has no physical unit or numeric range;
                # activity/completion eligibility is determined by the frozen
                # procedure state/time rule in SQL, not by optional status text.
                unit_valid = True
            elif rule == "mEq/L_or_mmol/L":
                unit_valid = source_unit in {"mEq/L", "mmol/L"}
            elif rule == "ratio":
                unit_valid = source_unit in {"", "ratio"}
            else:
                unit_valid = source_unit == rule
            minimum = _float(spec.get("min_value")); maximum = _float(spec.get("max_value"))
            if rule == "temperature_f":
                minimum = (minimum - 32.0) * 5.0 / 9.0 if minimum is not None else None
                maximum = (maximum - 32.0) * 5.0 / 9.0 if maximum is not None else None
            elif rule == "itemid223835_percent":
                minimum = minimum / 100.0 if minimum is not None else None
                maximum = maximum / 100.0 if maximum is not None else None
            range_valid = ((rule == "binary_activity" and number is None and bool(text_value))
                           or (minimum is None and maximum is None)
                           or (number is not None and (minimum is None or number >= minimum)
                               and (maximum is None or number <= maximum)))
            if not unit_valid:
                held.add(variable); hold_reasons[variable].add("INCLUDED_UNIT_VIOLATION")
            if not range_valid:
                held.add(variable); hold_reasons[variable].add("INCLUDED_RANGE_VIOLATION")
        if invalid:
            held.add(variable); hold_reasons[variable].add("INCLUDED_TIME_OR_CODE_VIOLATION")
    if enforce_contract_counts:
        expected_total = sum(int(value["record_count"]) for value in a2_contract_counts.values())
        if expected_total == 838803:
            raise ValueError("A2 audit-output row count cannot be used as the A3 value target")
        for contract, expected in a2_contract_counts.items():
            pair = tuple(observed[contract][:2])
            target = (int(expected["record_count"]), int(expected["included_count"]))
            if pair != target:
                raise ValueError(f"A3/A2 contract-count mismatch for {contract}: {pair}!={target}")
    return {
        "status": "PASS" if len(held) < len(registry_by_id) else "HOLD",
        "record_count": row_n,
        "time_included_count": sum(pair[1] for pair in observed.values()),
        "value_eligible_count": sum(pair[2] for pair in observed.values()),
        "value_exclusion_count": sum(pair[1] - pair[2] for pair in observed.values()),
        "included_count": sum(pair[2] for pair in observed.values()),
        "contract_counts": observed, "held_variables": sorted(held), "patient_n": len(patients),
        "exclusion_counts": exclusions,
        "warning_counts": warnings,
        "warning_count": sum(warnings.values()),
        "value_exclusion_patient_n": len(value_exclusion_patients),
        "value_exclusion_itemid_counts": dict(sorted(value_exclusion_itemids.items())),
        "value_exclusion_code_counts": dict(sorted(value_exclusion_codes.items())),
        "hold_reasons": {variable: sorted(reasons) for variable, reasons in sorted(hold_reasons.items())},
    }


def _identity_prefix(line: str) -> tuple[str, str]:
    first, sep, rest = line.partition(",")
    if not sep:
        raise ValueError("master row lacks hadm_id")
    second, sep, _opaque_tail = rest.partition(",")
    if not sep:
        raise ValueError("master row lacks nonidentity tail")
    return first.strip().strip('"'), second.strip().strip('"')


def guarded_master_rows(master_path: str | Path, allowed_ids: set[tuple[str, str]]) -> dict[str, Any]:
    development: list[dict[str, str]] = []
    temporal: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    with Path(master_path).open("r", encoding="utf-8-sig", newline="") as handle:
        header_line = handle.readline()
        header = next(csv.reader([header_line]))
        if header[:2] != ["subject_id", "hadm_id"]:
            raise ValueError("master identity columns must be first")
        for line in handle:
            identity = _identity_prefix(line)
            if identity in seen:
                raise ValueError("duplicate master identity")
            seen.add(identity)
            if identity not in allowed_ids:
                temporal.append(identity)
                continue
            values = next(csv.reader([line]))
            if len(values) != len(header):
                raise ValueError("development master row width mismatch")
            development.append(dict(zip(header, values)))
    return {"development_rows": development, "temporal_identities": sorted(temporal),
            "full_identity_n": len(seen)}


def expected_clinical_domain_map(registry: Iterable[Mapping[str, Any]],
                                 representation_spec: Iterable[Mapping[str, Any]]) -> list[dict[str, str]]:
    registry_rows = [dict(row) for row in registry]
    spec_rows = [dict(row) for row in representation_spec]
    variables_by_domain: dict[str, list[str]] = defaultdict(list)
    for row in registry_rows:
        variables_by_domain[str(row["domain"])].append(str(row["variable_id"]))
    columns_by_domain: Counter[str] = Counter()
    domain_by_variable = {str(row["variable_id"]): str(row["domain"]) for row in registry_rows}
    for row in spec_rows:
        columns_by_domain[domain_by_variable[str(row["variable_id"])]] += 1
    rows: list[dict[str, str]] = []
    for domain in TECHNICAL_TO_PROTOCOL:
        variables = variables_by_domain.get(domain, [])
        rows.append({
            "technical_domain": domain, "protocol_domain": TECHNICAL_TO_PROTOCOL[domain],
            "signal_count": str(len(variables)), "representation_column_count": str(columns_by_domain[domain]),
            "ordered_variable_ids": "/".join(variables), "registry_sha256": "",
            "representation_spec_sha256": "", "status": "PASS",
        })
    return rows


def validate_clinical_domain_map(registry: Iterable[Mapping[str, Any]],
                                 representation_spec: Iterable[Mapping[str, Any]],
                                 domain_map: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    expected = expected_clinical_domain_map(registry, representation_spec)
    observed = [dict(row) for row in domain_map]
    if len(observed) != 11 or [r["technical_domain"] for r in observed] != list(TECHNICAL_TO_PROTOCOL):
        raise ValueError("clinical domain identity mismatch")
    for actual, target in zip(observed, expected):
        for field in ("technical_domain", "protocol_domain", "signal_count", "representation_column_count", "ordered_variable_ids"):
            if str(actual.get(field, "")) != target[field]:
                raise ValueError(f"clinical domain map drift: {field}")
        if tuple(map(int, (actual["signal_count"], actual["representation_column_count"]))) != EXPECTED_DOMAIN_COUNTS[actual["technical_domain"]]:
            raise ValueError("clinical domain frozen count mismatch")
    if sum(int(r["signal_count"]) for r in observed) != 41 or sum(int(r["representation_column_count"]) for r in observed) != 157:
        raise ValueError("clinical domain totals do not close")
    return {"status": "PASS", "technical_domain_n": 11, "protocol_domain_n": 7,
            "signal_n": 41, "representation_column_n": 157}


def _float(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _hours(later: Any, earlier: Any) -> str:
    end, start = _dt(later), _dt(earlier)
    if not end or not start or end < start:
        return ""
    return format((end - start).total_seconds() / 3600.0, ".12g")


def build_c2_raw(identity_spine: Iterable[Mapping[str, Any]], master_path: str | Path | Iterable[Mapping[str, Any]],
                 p004_spec: str | Path) -> dict[str, Any]:
    _, p004 = _read_csv(p004_spec)
    source_fields: list[str] = []
    for row in p004:
        field = row["source_field"]
        if field not in source_fields:
            source_fields.append(field)
    if source_fields != C2_RAW_FIELDS:
        raise ValueError("P004 raw C2 field identity drift")
    spine = sorted((str(row["subject_id"]), str(row["hadm_id"])) for row in identity_spine)
    allowed = set(spine)
    if isinstance(master_path, (str, Path)):
        master_rows = guarded_master_rows(master_path, allowed)["development_rows"]
    else:
        master_rows = [dict(row) for row in master_path]
    by_id = {(str(row["subject_id"]), str(row["hadm_id"])): row for row in master_rows}
    if set(by_id) != allowed:
        raise ValueError("C2 raw master identities do not equal the development spine")
    rows = [{"subject_id": sid, "hadm_id": hid, **{field: by_id[(sid, hid)].get(field, "") for field in C2_RAW_FIELDS}}
            for sid, hid in spine]
    return {"fieldnames": ["subject_id", "hadm_id", *C2_RAW_FIELDS], "rows": rows}


def _master_extension_values(master: Mapping[str, Any]) -> dict[str, str]:
    t0 = master.get("t0", "")
    result = {
        "hours_since_icp_procedure": _hours(t0, master.get("icp_event_time")),
        "hours_since_cranial_operation": _hours(t0, master.get("cranial_event_time")),
        "gcsm_freshness_hours": _hours(t0, master.get("gcsm_availability_time")),
        "pupil_freshness_hours": _hours(t0, master.get("pupil_availability_time")),
        "wpr_freshness_hours": _hours(t0, master.get("wpr_availability_time")),
        "sofa_rollback_steps": str(master.get("sofa_rollback_steps", "")),
        "sofa_observed_domain_n": str(master.get("observed_domain_n", "")),
        "sofa_all_domains_unobserved_48h": str(master.get("all_domains_unobserved_48h", "")),
    }
    result["icp_procedure_recency_unavailable"] = "0" if result["hours_since_icp_procedure"] else "1"
    result["cranial_operation_recency_unavailable"] = "0" if result["hours_since_cranial_operation"] else "1"
    return result


def _choose(records: list[dict[str, Any]], latest: bool = False) -> dict[str, Any] | None:
    if not records:
        return None
    key = lambda r: (_dt(r["event_time"]), _dt(r["availability_time"]), str(r["record_key"]))
    return sorted(records, key=key, reverse=latest)[0]


def _trajectory_values(variable: str, records: list[dict[str, Any]], columns: list[str]) -> dict[str, str]:
    parsed = [dict(row, _number=_float(row.get("raw_numeric_value"))) for row in records]
    parsed = [row for row in parsed if row["_number"] is not None]
    if not parsed:
        return {}
    episode_start, t0 = _dt(parsed[0]["episode_start"]), _dt(parsed[0]["t0"])
    early = _choose([row for row in parsed if _dt(row["event_time"]) < min(episode_start + timedelta(hours=24), t0)])
    last = _choose([row for row in parsed if _dt(row["event_time"]) >= max(episode_start, t0 - timedelta(hours=48))], latest=True)
    low = min(parsed, key=lambda r: (r["_number"], _dt(r["event_time"]), str(r["record_key"])))
    high = max(parsed, key=lambda r: (r["_number"], _dt(r["event_time"]), str(r["record_key"])))
    result: dict[str, str] = {}
    for column in columns:
        if column.endswith("_early") and early:
            result[column] = format(early["_number"], ".12g")
        elif column.endswith("_minimum"):
            result[column] = format(low["_number"], ".12g")
        elif column.endswith("_maximum") or column.endswith("_highest"):
            result[column] = format(high["_number"], ".12g")
        elif column.endswith("_worst"):
            direction_low = variable in {"systolic_bp", "mean_arterial_pressure", "spo2", "platelet_absolute", "albumin"}
            result[column] = format((low if direction_low else high)["_number"], ".12g")
        elif column.endswith("_last") and last:
            result[column] = format(last["_number"], ".12g")
        elif column.endswith("_delta_last_minus_early") and early and last:
            result[column] = format(last["_number"] - early["_number"], ".12g")
    numeric = [column for column in columns if not column.endswith("_summary_unavailable") and not column.endswith("_last_freshness_hours")]
    unavailable = any(result.get(column, "") == "" for column in numeric)
    for column in columns:
        if column.endswith("_summary_unavailable"):
            result[column] = "1" if unavailable else "0"
        elif column.endswith("_last_freshness_hours"):
            result[column] = _hours(t0, last["availability_time"]) if last else ""
    return result


def build_extended_matrix(identity_spine: Iterable[Mapping[str, Any]], value_rows: Iterable[Mapping[str, Any]],
                          master_rows: Iterable[Mapping[str, Any]],
                          representation_spec: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    spec = [dict(row) for row in representation_spec]
    if len(spec) != 157 or len({row["column_name"] for row in spec}) != 157:
        raise ValueError("extended representation must contain exactly 157 unique columns")
    numeric_n = sum(row["data_type"] == "numeric" for row in spec)
    binary_n = sum(row["data_type"] == "binary" for row in spec)
    if (numeric_n, binary_n) != (121, 36):
        raise ValueError("extended representation numeric/binary counts drifted")
    spine = sorted((str(row["subject_id"]), str(row["hadm_id"])) for row in identity_spine)
    if len(spine) != len(set(spine)):
        raise ValueError("duplicate identity spine")
    columns = [row["column_name"] for row in spec]
    by_variable: dict[str, list[str]] = defaultdict(list)
    role_by_column = {row["column_name"]: row["column_role"] for row in spec}
    for row in spec:
        by_variable[row["variable_id"]].append(row["column_name"])
    output: dict[tuple[str, str], dict[str, str]] = {}
    for sid, hid in spine:
        row = {"subject_id": sid, "hadm_id": hid}
        for column in columns:
            role = role_by_column[column]
            row[column] = "1" if role == "unavailable" else ("0" if role == "categorical" else "")
        output[(sid, hid)] = row
    masters = {(str(row["subject_id"]), str(row["hadm_id"])): dict(row) for row in master_rows}
    for identity, master in masters.items():
        if identity not in output:
            raise PermissionError("master row outside identity spine")
        for column, value in _master_extension_values(master).items():
            if column in output[identity]:
                output[identity][column] = value
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for source in value_rows:
        row = dict(source)
        if not _truth(row.get("included")):
            continue
        identity = (str(row["subject_id"]), str(row["hadm_id"]))
        if identity not in output:
            raise PermissionError("value row outside identity spine")
        grouped[(identity[0], identity[1], str(row["variable_id"]))].append(row)
    for (sid, hid, variable), records in grouped.items():
        target = output[(sid, hid)]; variable_columns = by_variable.get(variable, [])
        if variable in {"rrt_ever", "rrt_active_t0", "hyperosmolar_ever", "sedative_active_t0",
                        "nmb_active_t0", "vasopressor_active_t0", "invasive_vent_active_t0"}:
            if variable in target:
                target[variable] = "1"
        elif variable in {"hours_since_vasopressor_end", "hours_since_invasive_vent_end"}:
            event = max((_dt(row["event_time"]) for row in records if _dt(row["event_time"])), default=None)
            value = _hours(records[0]["t0"], event.isoformat(sep=" ") if event else "")
            if variable in target:
                target[variable] = value
            unavailable = variable.replace("hours_since_", "") + "_recency_unavailable"
            if unavailable in target:
                target[unavailable] = "0" if value else "1"
        elif variable == "initial_gcs_total":
            times: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
            for row in records:
                number = _float(row.get("raw_numeric_value"))
                if number is not None and initial_gcs_assessable(row.get("status_text")):
                    times[row["event_time"]][row["itemid"]].append(number)
            complete = [(time, values) for time, values in times.items() if {"220739", "223900", "223901"} <= set(values)]
            if complete:
                _, values = sorted(complete)[0]
                target["initial_gcs_total_value"] = format(sum(min(values[item]) for item in ("220739", "223900", "223901")), ".12g")
                target["initial_gcs_unassessable"] = "0"
        elif variable == "initial_pupil_nonreactive_count":
            left = [r for r in records if r["itemid"] == "227288"]
            right = [r for r in records if r["itemid"] == "227121"]
            pairs = []
            for lrow in left:
                for rrow in right:
                    delta = abs((_dt(lrow["event_time"]) - _dt(rrow["event_time"])).total_seconds())
                    if delta <= 1800:
                        pairs.append((min(_dt(lrow["event_time"]), _dt(rrow["event_time"])), delta, lrow, rrow))
            if pairs:
                _, _, lrow, rrow = sorted(pairs, key=lambda item: (item[0], item[1], item[2]["record_key"], item[3]["record_key"]))[0]
                nonreactive = lambda r: int("non-reactive" in str(r.get("raw_text_value", "")).lower())
                target["initial_pupil_nonreactive_count_value"] = str(nonreactive(lrow) + nonreactive(rrow))
                target["initial_pupil_unassessable"] = "0"
        elif variable_columns:
            target.update(_trajectory_values(variable, records, variable_columns))
    return {"fieldnames": ["subject_id", "hadm_id", *columns],
            "rows": [output[key] for key in spine], "numeric_column_n": numeric_n,
            "binary_column_n": binary_n, "patient_n": len(spine)}
