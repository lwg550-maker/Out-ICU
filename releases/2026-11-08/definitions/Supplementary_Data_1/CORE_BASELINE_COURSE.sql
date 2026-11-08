SET default_transaction_read_only = on;
SET statement_timeout = '10min';
BEGIN READ ONLY;

COPY (
WITH cohort(subject_id, hadm_id, episode_start, t0) AS MATERIALIZED (
  VALUES __COHORT_VALUES__
), idx AS (
  SELECT c.*,a.admittime
  FROM cohort c JOIN mimiciv_hosp.admissions a USING(subject_id,hadm_id)
), prior_components AS (
  SELECT i.subject_id,
    max(ch.myocardial_infarct) myocardial_infarct,
    max(ch.congestive_heart_failure) congestive_heart_failure,
    max(ch.peripheral_vascular_disease) peripheral_vascular_disease,
    max(ch.cerebrovascular_disease) cerebrovascular_disease,
    max(ch.dementia) dementia,
    max(ch.chronic_pulmonary_disease) chronic_pulmonary_disease,
    max(ch.rheumatic_disease) rheumatic_disease,
    max(ch.peptic_ulcer_disease) peptic_ulcer_disease,
    max(ch.mild_liver_disease) mild_liver_disease,
    max(ch.diabetes_without_cc) diabetes_without_cc,
    max(ch.diabetes_with_cc) diabetes_with_cc,
    max(ch.paraplegia) paraplegia,
    max(ch.renal_disease) renal_disease,
    max(ch.malignant_cancer) malignant_cancer,
    max(ch.severe_liver_disease) severe_liver_disease,
    max(ch.metastatic_solid_tumor) metastatic_solid_tumor,
    max(ch.aids) aids,
    count(DISTINCT p.hadm_id) prior_admission_n,
    max(p.dischtime) AS latest_prior_dischtime
  FROM idx i
  JOIN mimiciv_hosp.admissions p ON p.subject_id=i.subject_id AND p.dischtime<i.admittime
  JOIN mimiciv_derived.charlson ch ON ch.hadm_id=p.hadm_id
  GROUP BY i.subject_id
), charlson AS (
  SELECT i.subject_id,coalesce(p.prior_admission_n,0) AS prior_admission_n,p.latest_prior_dischtime,
    coalesce(p.myocardial_infarct,0)+coalesce(p.congestive_heart_failure,0)+
    coalesce(p.peripheral_vascular_disease,0)+coalesce(p.cerebrovascular_disease,0)+
    coalesce(p.dementia,0)+coalesce(p.chronic_pulmonary_disease,0)+
    coalesce(p.rheumatic_disease,0)+coalesce(p.peptic_ulcer_disease,0)+
    CASE WHEN coalesce(p.severe_liver_disease,0)>0 THEN 3 WHEN coalesce(p.mild_liver_disease,0)>0 THEN 1 ELSE 0 END+
    CASE WHEN coalesce(p.diabetes_with_cc,0)>0 THEN 2 WHEN coalesce(p.diabetes_without_cc,0)>0 THEN 1 ELSE 0 END+
    2*coalesce(p.paraplegia,0)+2*coalesce(p.renal_disease,0)+
    CASE WHEN coalesce(p.metastatic_solid_tumor,0)>0 THEN 6 WHEN coalesce(p.malignant_cancer,0)>0 THEN 2 ELSE 0 END+
    6*coalesce(p.aids,0) AS charlson_prior_no_age
  FROM idx i LEFT JOIN prior_components p USING(subject_id)
), tx AS (
  SELECT c.subject_id,
    coalesce(sum(e.amount) FILTER (
      WHERE e.itemid IN (225168,226368,227070)
        AND e.amountuom='mL' AND e.amount>0
        AND e.statusdescription='FinishedRunning'
        AND e.endtime<=c.t0 AND e.storetime<=c.t0
    ),0) AS rbc_completed_ml,
    max(e.endtime) FILTER (
      WHERE e.itemid IN (225168,226368,227070)
        AND e.amountuom='mL' AND e.amount>0
        AND e.statusdescription='FinishedRunning'
        AND e.endtime<=c.t0 AND e.storetime<=c.t0
    ) AS rbc_event_time,
    max(greatest(e.endtime,e.storetime)) FILTER (
      WHERE e.itemid IN (225168,226368,227070)
        AND e.amountuom='mL' AND e.amount>0
        AND e.statusdescription='FinishedRunning'
        AND e.endtime<=c.t0 AND e.storetime<=c.t0
    ) AS rbc_availability_time,
    (count(*) FILTER (
      WHERE e.itemid IN (220970,225170,225171,226367,226369,227071)
        AND e.amount>0 AND e.starttime<c.t0 AND e.storetime<=c.t0
        AND e.statusdescription NOT IN ('Paused','Bolus')
    )>0)::int AS non_rbc_component_ever,
    max(e.starttime) FILTER (
      WHERE e.itemid IN (220970,225170,225171,226367,226369,227071)
        AND e.amount>0 AND e.starttime<c.t0 AND e.storetime<=c.t0
        AND e.statusdescription NOT IN ('Paused','Bolus')
    ) AS non_rbc_event_time,
    max(e.storetime) FILTER (
      WHERE e.itemid IN (220970,225170,225171,226367,226369,227071)
        AND e.amount>0 AND e.starttime<c.t0 AND e.storetime<=c.t0
        AND e.statusdescription NOT IN ('Paused','Bolus')
    ) AS non_rbc_availability_time
  FROM cohort c LEFT JOIN mimiciv_icu.inputevents e
    ON e.hadm_id=c.hadm_id AND e.starttime>=c.episode_start AND e.starttime<c.t0
   AND e.itemid IN (225168,226368,227070,220970,225170,225171,226367,226369,227071)
  GROUP BY c.subject_id
), vaso_raw AS (
  SELECT c.subject_id,c.t0,
         greatest(e.starttime,c.episode_start) AS s,
         least(e.endtime,c.t0) AS e,
         e.storetime
  FROM cohort c JOIN mimiciv_icu.inputevents e ON e.hadm_id=c.hadm_id
  WHERE e.itemid IN (221289,229617,221662,221653,221906,221749,229630,229631,229632,222315,229709,229764)
    AND e.starttime<c.t0 AND e.endtime>c.episode_start AND e.storetime<=c.t0
    AND coalesce(e.rate,e.amount,0)>0
    AND e.statusdescription NOT IN ('Paused','Bolus')
), vaso_mark AS (
  SELECT *,max(e) OVER (PARTITION BY subject_id ORDER BY s,e ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prev_max_e
  FROM vaso_raw WHERE e>s
), vaso_group AS (
  SELECT *,sum(CASE WHEN prev_max_e IS NULL OR s>prev_max_e THEN 1 ELSE 0 END)
    OVER (PARTITION BY subject_id ORDER BY s,e) AS grp
  FROM vaso_mark
), vaso_islands AS (
  SELECT subject_id,grp,min(s) s,max(e) e,max(storetime) availability_time
  FROM vaso_group GROUP BY subject_id,grp
), vaso AS (
  SELECT subject_id,
    extract(epoch FROM sum(e-s))/3600.0 AS vasopressor_active_hours,
    max(e) AS vasopressor_event_time,max(availability_time) AS vasopressor_availability_time
  FROM vaso_islands GROUP BY subject_id
), icp AS (
  SELECT c.subject_id,1 AS icp_monitor_or_csf_diversion_ever,
         max(p.endtime) AS icp_event_time,
         max(greatest(p.endtime,p.storetime)) AS icp_availability_time
  FROM cohort c JOIN mimiciv_icu.procedureevents p ON p.hadm_id=c.hadm_id
  WHERE p.itemid IN (225456,226124,226474,226475,229518,229521)
    AND p.endtime>=c.episode_start AND p.endtime<=c.t0 AND p.storetime<=c.t0
    AND coalesce(p.statusdescription,'') NOT IN ('Rewritten','Stopped')
  GROUP BY c.subject_id
), cranial_icd AS (
  SELECT c.subject_id,c.hadm_id,c.t0,p.chartdate,p.icd_version,p.icd_code,d.long_title,
    (
      (p.icd_version=9 AND replace(p.icd_code,'.','') IN ('0123','0124','0125','0131','0139'))
      OR
      (p.icd_version=10 AND d.long_title ~* '(extirpation|drainage).*(intracranial (subdural|epidural|subarachnoid)|cerebral hemisphere)' AND d.long_title ~* '(Open|Percutaneous) Approach')
    ) AS is_cranial_operation
  FROM cohort c JOIN mimiciv_hosp.procedures_icd p USING(subject_id,hadm_id)
  JOIN mimiciv_hosp.d_icd_procedures d USING(icd_version,icd_code)
), cranial_radiology AS (
  SELECT x.subject_id,x.hadm_id,x.t0,min(r.charttime) AS proxy_event_time,
         min(greatest(r.charttime,coalesce(r.storetime,r.charttime))) AS proxy_availability_time
  FROM cranial_icd x JOIN mimiciv_note.radiology r USING(subject_id,hadm_id)
  WHERE x.is_cranial_operation AND x.chartdate=x.t0::date
    AND r.charttime<=x.t0 AND coalesce(r.storetime,r.charttime)<=x.t0
    AND r.text ~* '(status post|s/p|postoperative|postsurgical|post-surgical)'
    AND r.text ~* '(craniotom|craniect|burr hole|subdural)'
  GROUP BY x.subject_id,x.hadm_id,x.t0
), cranial AS (
  SELECT c.subject_id,
    (count(*) FILTER (WHERE x.is_cranial_operation AND x.chartdate<c.t0::date)
     +count(*) FILTER (WHERE x.is_cranial_operation AND x.chartdate=c.t0::date AND r.subject_id IS NOT NULL)>0)::int AS cranial_operation_ever,
    max(CASE WHEN x.is_cranial_operation AND x.chartdate<c.t0::date THEN x.chartdate::timestamp
             WHEN x.is_cranial_operation AND x.chartdate=c.t0::date AND r.subject_id IS NOT NULL THEN r.proxy_event_time END) AS cranial_event_time,
    max(CASE WHEN x.is_cranial_operation AND x.chartdate<c.t0::date THEN (x.chartdate+1)::timestamp
             WHEN x.is_cranial_operation AND x.chartdate=c.t0::date AND r.subject_id IS NOT NULL THEN r.proxy_availability_time END) AS cranial_availability_time,
    (count(*) FILTER (WHERE x.is_cranial_operation AND x.chartdate=c.t0::date AND r.subject_id IS NULL)>0)::int AS cranial_same_day_unconfirmed
  FROM cohort c LEFT JOIN cranial_icd x ON x.subject_id=c.subject_id
  LEFT JOIN cranial_radiology r ON r.subject_id=x.subject_id
  GROUP BY c.subject_id
)
SELECT c.subject_id,c.hadm_id,
       ch.prior_admission_n,ch.charlson_prior_no_age,ch.latest_prior_dischtime AS charlson_event_time,
       ch.latest_prior_dischtime AS charlson_availability_time,
       tx.rbc_completed_ml,ln(1+tx.rbc_completed_ml) AS log1p_rbc_completed_ml,
       tx.rbc_event_time,tx.rbc_availability_time,
       tx.non_rbc_component_ever,tx.non_rbc_event_time,tx.non_rbc_availability_time,
       coalesce(v.vasopressor_active_hours,0) AS vasopressor_active_hours,
       ln(1+coalesce(v.vasopressor_active_hours,0)) AS log1p_vasopressor_active_hours,
       v.vasopressor_event_time,v.vasopressor_availability_time,
       coalesce(i.icp_monitor_or_csf_diversion_ever,0) AS icp_monitor_or_csf_diversion_ever,
       i.icp_event_time,i.icp_availability_time,
       coalesce(cr.cranial_operation_ever,0) AS cranial_operation_ever,
       cr.cranial_event_time,cr.cranial_availability_time,coalesce(cr.cranial_same_day_unconfirmed,0) AS cranial_same_day_unconfirmed,
       extract(epoch FROM (c.t0-c.episode_start))/3600.0 AS icu_episode_hours,
       ln(1+extract(epoch FROM (c.t0-c.episode_start))/3600.0) AS log1p_icu_episode_hours,
       c.t0 AS icu_episode_event_time,c.t0 AS icu_episode_availability_time
FROM cohort c
JOIN charlson ch USING(subject_id)
JOIN tx USING(subject_id)
LEFT JOIN vaso v USING(subject_id)
LEFT JOIN icp i USING(subject_id)
LEFT JOIN cranial cr USING(subject_id)
ORDER BY c.subject_id
) TO STDOUT WITH (FORMAT CSV, HEADER TRUE);

ROLLBACK;
