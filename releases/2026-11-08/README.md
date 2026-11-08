# Out-ICU: corrected-cohort research release (2026-11-08)

Adults with acute TBI surviving the first eligible ICU episode; t0 is its endpoint. Corrected N=3,244; development n=2,838 (90/180-day deaths 359/435); temporal n=406 (59/68). Display name M1_EN maps to the historical M0 implementation; filenames do not indicate new fits.

## Files and manuscript mapping

- `tables/`: complete performance and paired comparisons.
- `source_data/`: aggregate source data for main/supplementary figures, not patient records.
- `definitions/Supplementary_Data_1/`: manuscript Supplementary Data 1, including SQL and variable/time contracts.
- `code/src`, `code/sql`, `code/config`: screened scientific implementation and fixed definitions.
- `code/tools/research_release_20261106/`: original calculator/interface and temporal evaluation code for inspection, without the restricted objects required to run them.
- `docs/ALGORITHM_S1.md`: exact tie, dependency, budget and negative-importance rules.
- `docs/REPRODUCIBILITY.md`: executable versus restricted scope.
- `MANIFEST_SHA256.json`: checksums for this public release.

## Run without patient data

From this directory, with Python 3.10 or newer:

```sh
python verify_public.py
python test_selection_rules.py
```

The first verifies the release inventory and all hashes; the second tests the actual selection functions on artificial, nonpatient inputs. Neither command fits a predictive model, resamples study patients, connects to a database, or requires MIMIC access. They are NOT end-to-end validation of the clinical prediction engine.

## Access boundaries

Source MIMIC-IV 3.1 requires PhysioNet credentialing, training and the data use agreement: https://doi.org/10.13026/kpb9-mt58 . The public release excludes patient-level tables, identities, split assignments, predictions, SHAP backgrounds/contributions, fitted models, personal signatures and access credentials. PhysioNet requires sensitive derived datasets/models to be shared under the source agreement: https://physionet.org/content/mimiciv/3.1/ .

No controlled-access deposit of the frozen model artifacts currently exists. The calculator is not runnable from this public repository alone. Credentialed reviewers/researchers may contact the corresponding authors listed in the manuscript to discuss an agreement-compliant route; contact does not itself authorize transfer or guarantee access. Do not distribute model files through GitHub or email merely because a recipient is credentialed. A formal PhysioNet-compatible sharing route remains to be established.

The public code is a screened implementation archive, not a standalone replay-tested pipeline. Some historical authorization/hash dependencies and restricted artifacts are intentionally absent. Requirements and configuration descriptions are supplied for inspection; installing dependencies alone cannot resolve missing data/artifacts. Research scripts must not be pointed at an original database without appropriate access and read-only safeguards. No fitting was performed for this release.

## Rights and third-party resources

The existing repository MIT license is preserved for the author's software. It does not license MIMIC data, fitted derived resources, third-party dependencies, or third-party content; their original terms remain applicable. No patient-data redistribution rights are granted.
