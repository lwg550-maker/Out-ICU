# Supplementary Algorithm S1. Dependency-aware field-budget selection

1. Within each training partition, perform two-fold outcome-stratified cross-fitting with the fixed GBST ranker. For each active clinical group, jointly permute its transformed columns five times in the held-out portion. Derived encodings and missingness states belonging to the group move together.
2. For each group, average the increase in 180-day Brier score over its available fold/permutation records, weighting each record by its held-out sample size. Groups with no active columns in either fold have no importance evidence and are inactive.
3. Sort groups by decreasing importance. Resolve exact numerical ties using their fixed order in COMPACT_MANUAL_INPUT_SPEC.json. Retain negative importance values; no sign or significance filter is applied.
4. For each budget of 6, 10, or 14 fields independently, initialize an empty selected set. Visit every ranked group once. Compute the number of distinct manual fields required by the union of selected groups and the proposed group. Shared dependencies count once, not once per group; transformations and model-column counts do not determine cost.
5. If the union fits within the budget, add the group. Otherwise skip it and continue to lower-ranked groups. Do not stop at the first over-budget group or truncate a group. Continue through the full ranking, including negative-importance groups that fit. The budget is a maximum, not a requirement to fill every field.
6. Use each selected set in the original five-fold inner candidate-grid search and outer evaluation. Repeat ranking within the relevant training partition; never rank using held-out outer or temporal patients. This greedy procedure does not claim globally optimal subset search.

Implementation: code/src/project_b/compact_selection.py, aggregate_importance and rank_partition; field unions: compact_inputs.py, selected_manual_fields. The frozen group order and dependencies are in code/config/COMPACT_MANUAL_INPUT_SPEC.json. Public synthetic tests exercise ties, skipping, shared dependencies, negative importance, and inactive groups without patient data or predictive fitting.

