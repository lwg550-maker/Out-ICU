# Calculator applicability and restricted dependencies

The locked 10-input model is a research prototype, not a clinical recommendation. Its GCS-M 1–3 indicator was unused in tree splits and has zero SHAP contribution. With all other inputs held fixed, this motor-score encoding cannot distinguish GCS-M 1–3 from the reference of 6. Different patients can nevertheless have different predictions because age, initial GCS, and other inputs differ. Applicability to persistently low motor responses needs particular evaluation. Do not modify the locked model to impose an expected direction.

Input eligibility is a requirement on users, not a tested observation of real-world acquisition. Users must check event time, availability time, GCS reliability and the different paired-versus-latest white-cell rules. The software does not retrieve or audit original EHR timestamps. Computational parity tests do not establish input reproducibility or clinical benefit.

Model weights, fitted preprocessing and authorization bindings are not publicly included. No controlled-access model deposit currently exists. The public calculator source cannot run alone; controlled sharing must follow the original MIMIC agreement and PhysioNet guidance. Do not load untrusted pickle objects.
