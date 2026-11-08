SET default_transaction_read_only = on;
SET statement_timeout = '10min';
BEGIN READ ONLY;

COPY (
WITH dict AS (
  SELECT icd_version,icd_code,upper(replace(trim(icd_code),'.','')) AS code
  FROM mimiciv_hosp.d_icd_diagnoses
), matched AS (
  SELECT icd_version,icd_code FROM dict
  WHERE (icd_version=9 AND (
      (left(code,3) IN ('800','801','803','804') AND
       (substring(code,4,1) IN ('1','2','3','4','6','7','8','9') OR
        substring(code,4,2) IN ('03','04','05','53','54','55','00','02','06','09','50','52','56','59')))
      OR (left(code,3)='850' AND substring(code,4,1) IN ('0','1','2','3','4','5','9'))
      OR left(code,3) IN ('851','852','853')
      OR (left(code,3)='854' AND substring(code,4,1) IN ('0','1'))
      OR left(code,4) IN ('9501','9502','9503')
    ))
    OR (icd_version=10 AND (left(code,3)='S06' OR left(code,5) IN ('S0402','S0403','S0404'))
        AND substring(code,7,1)='A')
), tbi_hadm AS (
  SELECT DISTINCT d.subject_id,d.hadm_id
  FROM mimiciv_hosp.diagnoses_icd d JOIN matched m USING(icd_version,icd_code)
), index_admission AS (
  SELECT * FROM (
    SELECT t.subject_id,t.hadm_id,a.admittime,a.dischtime,a.deathtime,
           a.hospital_expire_flag,a.discharge_location,p.dod,p.gender,p.anchor_year_group,
           p.anchor_age+extract(year FROM a.admittime)::int-p.anchor_year AS age_at_admit,
           row_number() OVER(PARTITION BY t.subject_id ORDER BY a.admittime,a.hadm_id) AS rn
    FROM tbi_hadm t
    JOIN mimiciv_hosp.admissions a USING(subject_id,hadm_id)
    JOIN mimiciv_hosp.patients p USING(subject_id)
    WHERE p.anchor_age+extract(year FROM a.admittime)::int-p.anchor_year>=18
      AND EXISTS (SELECT 1 FROM mimiciv_icu.icustays i
                  WHERE i.subject_id=t.subject_id AND i.hadm_id=t.hadm_id)
  ) q WHERE rn=1
), stay_order AS (
  SELECT i.*,x.admittime,x.dischtime,x.deathtime,x.hospital_expire_flag,
         x.discharge_location,x.dod,x.gender,x.anchor_year_group,x.age_at_admit,
         max(i.outtime) OVER(PARTITION BY i.hadm_id ORDER BY i.intime,i.outtime NULLS LAST,i.stay_id
           ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prior_max_outtime
  FROM mimiciv_icu.icustays i JOIN index_admission x USING(subject_id,hadm_id)
), numbered AS (
  SELECT s.*,
         sum(CASE WHEN prior_max_outtime IS NULL OR intime>prior_max_outtime+interval '6 hours'
                  THEN 1 ELSE 0 END)
         OVER(PARTITION BY hadm_id ORDER BY intime,outtime NULLS LAST,stay_id
              ROWS UNBOUNDED PRECEDING) AS episode_no
  FROM stay_order s
), episode AS (
  SELECT subject_id,hadm_id,episode_no,array_agg(stay_id ORDER BY intime,stay_id) AS stay_ids,
         min(intime) AS episode_start,max(outtime) AS t0,
         count(*) FILTER(WHERE outtime IS NULL) AS missing_outtime,
         min(admittime) AS admittime,min(dischtime) AS dischtime,min(deathtime) AS deathtime,
         min(hospital_expire_flag) AS hospital_expire_flag,min(discharge_location) AS discharge_location,
         min(dod) AS dod,min(gender) AS gender,min(anchor_year_group) AS anchor_year_group,
         min(age_at_admit) AS age_at_admit
  FROM numbered WHERE episode_no=1
  GROUP BY subject_id,hadm_id,episode_no
), brain_death AS (
  SELECT hadm_id,min(greatest(starttime,storetime)) AS first_known_time
  FROM mimiciv_icu.procedureevents WHERE itemid=225819 GROUP BY hadm_id
), donor_perfusion AS (
  SELECT DISTINCT hadm_id FROM mimiciv_hosp.procedures_icd
  WHERE icd_version=10 AND icd_code IN ('6AB50BZ','6ABB0BZ','6ABF0BZ','6ABT0BZ')
), cohort AS (
  SELECT e.*
  FROM episode e LEFT JOIN brain_death b USING(hadm_id) LEFT JOIN donor_perfusion p USING(hadm_id)
  WHERE e.missing_outtime=0 AND e.t0 IS NOT NULL
    AND (e.deathtime>e.t0 OR (e.deathtime IS NULL AND (e.dod IS NULL OR e.dod>e.t0::date)))
    AND NOT (b.hadm_id IS NOT NULL AND b.first_known_time<=e.t0)
    AND p.hadm_id IS NULL
)
SELECT subject_id,hadm_id,array_to_string(stay_ids,';') AS stay_ids,
       episode_start,t0,admittime,dischtime,deathtime,dod,
       CASE WHEN deathtime IS NOT NULL THEN 'deathtime'
            WHEN dod IS NOT NULL THEN 'dod_date' ELSE 'none' END AS death_source,
       gender,age_at_admit,anchor_year_group,hospital_expire_flag,discharge_location,
       (dischtime<t0)::int AS dischtime_before_t0,
       (t0+interval '90 days'<=dischtime+interval '365 days')::int AS admin_90_eligible,
       (t0+interval '180 days'<=dischtime+interval '365 days')::int AS admin_180_eligible,
       CASE WHEN deathtime IS NOT NULL THEN
              (deathtime>t0 AND deathtime<=t0+interval '90 days')::int
            ELSE (dod IS NOT NULL AND dod>t0::date AND dod<=(t0+interval '90 days')::date)::int END AS death_90d,
       CASE WHEN deathtime IS NOT NULL THEN
              (deathtime>t0 AND deathtime<=t0+interval '180 days')::int
            ELSE (dod IS NOT NULL AND dod>t0::date AND dod<=(t0+interval '180 days')::date)::int END AS death_180d,
       CASE WHEN deathtime IS NOT NULL THEN extract(epoch FROM (deathtime-t0))/86400.0
            WHEN dod IS NOT NULL THEN (dod-t0::date)::numeric ELSE NULL END AS death_days_after_t0,
       CASE WHEN deathtime IS NOT NULL THEN (deathtime=t0+interval '90 days')::int
            ELSE (dod=(t0+interval '90 days')::date)::int END AS exact_90_boundary,
       CASE WHEN deathtime IS NOT NULL THEN (deathtime=t0+interval '180 days')::int
            ELSE (dod=(t0+interval '180 days')::date)::int END AS exact_180_boundary
FROM cohort
ORDER BY subject_id
) TO STDOUT WITH (FORMAT CSV, HEADER TRUE);

ROLLBACK;
