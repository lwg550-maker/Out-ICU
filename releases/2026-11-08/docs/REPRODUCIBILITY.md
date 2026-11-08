# Reproducibility and access matrix

| Task | Publicly available | Executed in this release | Missing requirement |
| --- | --- | --- | --- |
| Verify aggregate files and release hashes | Yes | Yes | None |
| Test selection ties/budget/dependency logic | Yes | Yes, artificial inputs | Python only |
| Inspect extraction and model-development code | Screened source subset | Syntax/static checks only | Authorized database, full controlled workspace and original bindings |
| Recreate original training/validation | No claim of standalone executability | No | Authorized patient data and restricted dependencies; substantial training |
| Run locked clinical calculator | Source/interface only | No clinical run in public release | Frozen fitted model, preprocessing, original lock and bindings, compatible environment |
| Independently reproduce patient-level SHAP | No | No | Authorized development data, background and locked objects |

Full fitting originally used Python 3.12.14, lifelines 0.30.0, scikit-survival 0.28.0 and scikit-learn 1.9.1; SHAP used 0.54.1. The public synthetic and hash checks use only Python standard-library modules. These runtime statements are not evidence that a clean-machine end-to-end reconstruction has passed.

Calculator security: do not unpickle untrusted model files. Any future controlled model release must preserve the frozen model/preprocessing/lock version and verify hashes before use. No retraining or replacement model should be substituted to make this public archive appear runnable.

Bootstrap intervals use only common-valid draws; failed attempts are retained in logs, not interval samples. See temporal_release.py for exact validation and retained-output behavior. The realized study had 2,000 common-valid draws with no rejection.
