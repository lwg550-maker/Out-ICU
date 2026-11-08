SET default_transaction_read_only = on;
SET statement_timeout = '10min';
BEGIN READ ONLY;
SET LOCAL enable_seqscan = off;
SET LOCAL enable_hashjoin = off;

COPY (
WITH cohort(subject_id, hadm_id, episode_start, t0) AS MATERIALIZED (
  VALUES __COHORT_VALUES__
), stays AS MATERIALIZED (
  SELECT c.subject_id,c.hadm_id,c.episode_start,c.t0,i.stay_id
  FROM cohort c JOIN mimiciv_icu.icustays i USING(subject_id,hadm_id)
  WHERE i.intime<c.t0 AND coalesce(i.outtime,c.t0)>c.episode_start
), candidates AS MATERIALIZED (
  SELECT s.subject_id,s.hadm_id,s.t0,s.stay_id,v.ventilation_status,
         greatest(v.starttime,s.episode_start) AS candidate_start,
         least(v.endtime,s.t0) AS candidate_end
  FROM stays s JOIN mimiciv_derived.ventilation v USING(stay_id)
  WHERE v.ventilation_status IN ('InvasiveVent','Tracheostomy')
    AND v.starttime<s.t0 AND v.endtime>s.episode_start
), raw AS MATERIALIZED (
  SELECT s.subject_id,s.hadm_id,s.episode_start,s.t0,s.stay_id,
         ce.charttime,max(ce.storetime) AS storetime,
         bool_or(ce.itemid=223849 AND lower(trim(ce.value))='standby') AS standby,
         bool_or(ce.itemid IN (223849,229314) AND ce.value IN (
           '(S) CMV','APRV','APRV/Biphasic+ApnPress','APRV/Biphasic+ApnVol','APV (cmv)',
           'Ambient','Apnea Ventilation','CMV','CMV/ASSIST','CMV/ASSIST/AutoFlow','CMV/AutoFlow',
           'CPAP/PPS','CPAP/PSV','CPAP/PSV+Apn TCPL','CPAP/PSV+ApnPres','CPAP/PSV+ApnVol',
           'MMV','MMV/AutoFlow','MMV/PSV','MMV/PSV/AutoFlow','P-CMV','PCV+','PCV+/PSV',
           'PCV+Assist','PRES/AC','PRVC/AC','PRVC/SIMV','PSV/SBT','SIMV','SIMV/AutoFlow',
           'SIMV/PRES','SIMV/PSV','SIMV/PSV/AutoFlow','SIMV/VOL','SYNCHRON MASTER',
           'SYNCHRON SLAVE','VOL/AC','APV (simv)','P-SIMV','VS','ASV'
         )) AS invasive_mode,
         bool_or(ce.itemid IN (224688,224684,224685,224686,224696,220339,224700)
                 AND ce.valuenum>0) AS positive_pressure_setting,
         bool_or(ce.itemid=226732 AND trim(ce.value) IN ('Tracheostomy tube','Trach mask','T-piece')) AS trach_route_or_nonpositive_device
  FROM stays s
  CROSS JOIN LATERAL (
    SELECT itemid,charttime,storetime,value,valuenum
    FROM mimiciv_icu.chartevents ce
    WHERE ce.stay_id=s.stay_id
      AND ce.itemid IN (223849,229314,224688,224684,224685,224686,224696,220339,224700,226732)
      AND ce.charttime>=s.episode_start AND ce.charttime<s.t0
      AND greatest(ce.charttime,ce.storetime)<=s.t0
  ) ce
  GROUP BY s.subject_id,s.hadm_id,s.episode_start,s.t0,s.stay_id,ce.charttime
), classified AS (
  SELECT *,greatest(charttime,storetime) AS availability_time,
    CASE WHEN standby THEN 'nonactive'
         WHEN invasive_mode OR positive_pressure_setting THEN 'active'
         WHEN trach_route_or_nonpositive_device THEN 'nonactive'
         ELSE 'other' END AS evidence_state
  FROM raw
), ordered AS (
  SELECT *,min(charttime) FILTER (WHERE evidence_state='nonactive')
    OVER (PARTITION BY subject_id ORDER BY charttime,stay_id ROWS BETWEEN 1 FOLLOWING AND UNBOUNDED FOLLOWING) AS next_nonactive
  FROM classified
), raw_active_seed AS (
  SELECT subject_id,hadm_id,stay_id,t0,charttime AS s,
         least(charttime+interval '14 hours',coalesce(next_nonactive,charttime+interval '14 hours'),t0) AS e,
         availability_time
  FROM ordered WHERE evidence_state='active'
), active_seed AS (
  SELECT r.subject_id,r.hadm_id,r.t0,
         greatest(r.s,c.candidate_start) AS s,
         least(r.e,c.candidate_end) AS e,
         r.availability_time
  FROM raw_active_seed r JOIN candidates c
    ON c.subject_id=r.subject_id AND c.stay_id=r.stay_id
   AND r.s<c.candidate_end AND r.e>c.candidate_start
), marked AS (
  SELECT *,max(e) OVER (PARTITION BY subject_id ORDER BY s,e ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prev_max_e
  FROM active_seed WHERE e>s
), grouped AS (
  SELECT *,sum(CASE WHEN prev_max_e IS NULL OR s>prev_max_e THEN 1 ELSE 0 END)
    OVER (PARTITION BY subject_id ORDER BY s,e) AS grp
  FROM marked
), islands AS (
  SELECT subject_id,grp,min(s) s,max(e) e,max(availability_time) source_availability_time
  FROM grouped GROUP BY subject_id,grp
), vent AS (
  SELECT subject_id,extract(epoch FROM sum(e-s))/3600.0 AS invasive_ventilation_hours,
         max(e) AS ventilation_event_time,max(source_availability_time) AS ventilation_source_availability_time,
         count(*) AS merged_interval_n
  FROM islands GROUP BY subject_id
), trach AS (
  SELECT s.subject_id,count(*) AS tracheostomy_candidate_segment_n
  FROM stays s JOIN mimiciv_derived.ventilation v USING(stay_id)
  WHERE v.ventilation_status='Tracheostomy' AND v.starttime<s.t0 AND v.endtime>s.episode_start
  GROUP BY s.subject_id
)
SELECT c.subject_id,c.hadm_id,
       coalesce(v.invasive_ventilation_hours,0) AS cumulative_invasive_ventilation_hours,
       ln(1+coalesce(v.invasive_ventilation_hours,0)) AS log1p_cumulative_invasive_ventilation_hours,
       v.ventilation_event_time,
       CASE WHEN v.subject_id IS NOT NULL THEN c.t0 END AS ventilation_availability_time,
       v.ventilation_source_availability_time,coalesce(v.merged_interval_n,0) AS merged_interval_n,
       coalesce(t.tracheostomy_candidate_segment_n,0) AS tracheostomy_candidate_segment_n
FROM cohort c LEFT JOIN vent v USING(subject_id) LEFT JOIN trach t USING(subject_id)
ORDER BY c.subject_id
) TO STDOUT WITH (FORMAT CSV, HEADER TRUE);

ROLLBACK;
