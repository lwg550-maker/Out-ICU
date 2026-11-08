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
), pupil_raw AS MATERIALIZED (
  SELECT s.subject_id,s.t0,ce.itemid,ce.charttime,max(ce.storetime) AS storetime,
    max(CASE lower(trim(ce.value)) WHEN 'non-reactive' THEN 1 WHEN 'sluggish' THEN 0 WHEN 'brisk' THEN 0 END) AS nonreactive,
    max(CASE lower(trim(ce.value)) WHEN 'non-reactive' THEN 2 WHEN 'sluggish' THEN 1 WHEN 'brisk' THEN 0 END) AS sluggish_sensitivity_score
  FROM stays s CROSS JOIN LATERAL (
    SELECT itemid,charttime,storetime,value
    FROM mimiciv_icu.chartevents ce
    WHERE ce.stay_id=s.stay_id AND ce.itemid IN (227121,227288)
      AND ce.charttime>=s.t0-interval '6 hours' AND ce.charttime<s.t0
      AND greatest(ce.charttime,ce.storetime)<=s.t0
      AND lower(trim(ce.value)) IN ('brisk','sluggish','non-reactive')
  ) ce
  GROUP BY s.subject_id,s.t0,ce.itemid,ce.charttime
), pupil_anchors AS (
  SELECT p.*,
         q.charttime AS other_charttime,q.storetime AS other_storetime,
         q.nonreactive AS other_nonreactive,q.sluggish_sensitivity_score AS other_sluggish_score
  FROM pupil_raw p
  LEFT JOIN LATERAL (
    SELECT q.* FROM pupil_raw q
    WHERE q.subject_id=p.subject_id AND q.itemid<>p.itemid
      AND abs(extract(epoch FROM (q.charttime-p.charttime)))<=1800
    ORDER BY abs(extract(epoch FROM (q.charttime-p.charttime))),q.charttime DESC
    LIMIT 1
  ) q ON true
), pupil_rank AS (
  SELECT *,row_number() OVER (PARTITION BY subject_id ORDER BY greatest(charttime,coalesce(other_charttime,charttime)) DESC,
             (other_charttime IS NOT NULL) DESC,charttime DESC,itemid) AS rn
  FROM pupil_anchors
), pupil AS (
  SELECT subject_id,
    (nonreactive+coalesce(other_nonreactive,0))::int AS nonreactive_pupil_count,
    (other_charttime IS NULL)::int AS pupil_unassessable,
    greatest(charttime,coalesce(other_charttime,charttime)) AS pupil_event_time,
    greatest(storetime,coalesce(other_storetime,storetime),charttime,coalesce(other_charttime,charttime)) AS pupil_availability_time,
    itemid AS anchor_itemid,charttime AS anchor_charttime,other_charttime,
    (sluggish_sensitivity_score+coalesce(other_sluggish_score,0))::int AS pupil_sluggish_sensitivity_score
  FROM pupil_rank WHERE rn=1
), gcs_raw AS MATERIALIZED (
  SELECT s.subject_id,s.hadm_id,s.t0,ce.charttime,max(ce.storetime) AS storetime,min(ce.valuenum)::int AS gcsm
  FROM stays s CROSS JOIN LATERAL (
    SELECT charttime,storetime,valuenum
    FROM mimiciv_icu.chartevents ce
    WHERE ce.stay_id=s.stay_id AND ce.itemid=223901
      AND ce.charttime>=s.t0-interval '6 hours' AND ce.charttime<s.t0
      AND ce.valuenum BETWEEN 1 AND 6
      AND greatest(ce.charttime,ce.storetime)<=s.t0
  ) ce
  GROUP BY s.subject_id,s.hadm_id,s.t0,ce.charttime
), rass_deep AS MATERIALIZED (
  SELECT DISTINCT s.subject_id,ce.charttime
  FROM stays s CROSS JOIN LATERAL (
    SELECT charttime FROM mimiciv_icu.chartevents ce
    WHERE ce.stay_id=s.stay_id AND ce.itemid=228096
      AND ce.charttime>=s.t0-interval '6 hours' AND ce.charttime<s.t0
      AND ce.valuenum<=-4 AND greatest(ce.charttime,ce.storetime)<=s.t0
  ) ce
), nmb_intervals AS MATERIALIZED (
  SELECT c.subject_id,e.starttime,e.endtime
  FROM cohort c JOIN mimiciv_icu.inputevents e ON e.hadm_id=c.hadm_id
  WHERE e.itemid IN (221555,222062,229233)
    AND e.starttime<c.t0 AND e.endtime>c.t0-interval '6 hours'
    AND e.storetime<=c.t0 AND coalesce(e.rate,e.amount,0)>0
    AND e.statusdescription NOT IN ('Paused','Bolus')
), sedation_intervals AS MATERIALIZED (
  SELECT c.subject_id,e.starttime,e.endtime
  FROM cohort c JOIN mimiciv_icu.inputevents e ON e.hadm_id=c.hadm_id
  WHERE e.itemid IN (221668,222168,225150,229420)
    AND e.starttime<c.t0 AND e.endtime>c.t0-interval '6 hours'
    AND e.storetime<=c.t0 AND coalesce(e.rate,e.amount,0)>0
    AND e.statusdescription NOT IN ('Paused','Bolus')
), gcs_interference AS (
  SELECT g.*,
    EXISTS (
      SELECT 1 FROM nmb_intervals e
      WHERE e.subject_id=g.subject_id AND e.starttime<=g.charttime AND e.endtime>g.charttime
    ) AS nmb_active,
    (
      EXISTS (
        SELECT 1 FROM rass_deep r
        WHERE r.subject_id=g.subject_id AND r.charttime=g.charttime
      )
      AND EXISTS (
        SELECT 1 FROM sedation_intervals e
        WHERE e.subject_id=g.subject_id AND e.starttime<=g.charttime AND e.endtime>g.charttime
      )
    ) AS deep_sedation_active
  FROM gcs_raw g
), gcs_reliable AS (
  SELECT DISTINCT ON (subject_id) subject_id,gcsm,charttime,storetime
  FROM gcs_interference WHERE NOT nmb_active AND NOT deep_sedation_active
  ORDER BY subject_id,charttime DESC
), gcs_any AS (
  SELECT subject_id,count(*) AS candidate_n,
         count(*) FILTER (WHERE nmb_active OR deep_sedation_active) AS interfered_n,
         max(charttime) AS latest_candidate_time,max(storetime) AS latest_candidate_availability
  FROM gcs_interference GROUP BY subject_id
), gcs AS (
  SELECT c.subject_id,
    CASE WHEN r.gcsm=6 THEN '6'
         WHEN r.gcsm BETWEEN 4 AND 5 THEN '4-5'
         WHEN r.gcsm BETWEEN 1 AND 3 THEN '1-3'
         ELSE 'unassessable' END AS gcsm_category,
    coalesce((r.gcsm BETWEEN 4 AND 5)::int,0) AS gcsm_4_5,
    coalesce((r.gcsm BETWEEN 1 AND 3)::int,0) AS gcsm_1_3,
    (r.gcsm IS NULL)::int AS gcsm_unassessable,
    r.gcsm AS selected_gcsm,r.charttime AS gcsm_event_time,
    greatest(r.charttime,r.storetime) AS gcsm_availability_time,
    coalesce(a.candidate_n,0) AS gcsm_candidate_n,coalesce(a.interfered_n,0) AS gcsm_interfered_n,
    CASE WHEN r.gcsm IS NOT NULL THEN 'reliable'
         WHEN coalesce(a.candidate_n,0)=0 THEN 'no_record'
         ELSE 'all_candidates_treatment_interfered' END AS gcsm_assessability_reason
  FROM cohort c LEFT JOIN gcs_reliable r USING(subject_id) LEFT JOIN gcs_any a USING(subject_id)
), wpr_specimens AS MATERIALIZED (
  SELECT c.subject_id,c.t0,le.specimen_id,le.charttime,
    max(le.valuenum) FILTER (WHERE le.itemid=51301 AND le.valueuom='K/uL' AND le.valuenum>0) AS wbc,
    max(le.valuenum) FILTER (WHERE le.itemid=51265 AND le.valueuom='K/uL' AND le.valuenum>0) AS platelet,
    max(le.storetime) FILTER (WHERE le.itemid=51301) AS wbc_storetime,
    max(le.storetime) FILTER (WHERE le.itemid=51265) AS platelet_storetime
  FROM cohort c CROSS JOIN LATERAL (
    SELECT specimen_id,charttime,storetime,itemid,valuenum,valueuom
    FROM mimiciv_hosp.labevents le
    WHERE le.subject_id=c.subject_id AND le.hadm_id=c.hadm_id
      AND le.itemid IN (51301,51265)
      AND le.charttime>=c.t0-interval '24 hours' AND le.charttime<c.t0
      AND greatest(le.charttime,le.storetime)<=c.t0
  ) le
  GROUP BY c.subject_id,c.t0,le.specimen_id,le.charttime
), wpr_valid AS (
  SELECT *,row_number() OVER (PARTITION BY subject_id ORDER BY charttime DESC,specimen_id DESC) AS rn
  FROM wpr_specimens
  WHERE wbc>0 AND platelet>0 AND wbc_storetime<=t0 AND platelet_storetime<=t0
), wpr AS (
  SELECT subject_id,wbc,platelet,wbc/platelet AS wpr,ln(wbc/platelet)/ln(2.0) AS log2_wpr,
         charttime AS wpr_event_time,
         greatest(charttime,wbc_storetime,platelet_storetime) AS wpr_availability_time,
         specimen_id
  FROM wpr_valid WHERE rn=1
)
SELECT c.subject_id,c.hadm_id,
       coalesce(p.nonreactive_pupil_count,0) AS nonreactive_pupil_count,
       coalesce(p.pupil_unassessable,1) AS pupil_unassessable,
       p.pupil_event_time,p.pupil_availability_time,p.anchor_itemid,p.anchor_charttime,p.other_charttime,
       p.pupil_sluggish_sensitivity_score,
       g.gcsm_category,g.gcsm_4_5,g.gcsm_1_3,g.gcsm_unassessable,g.selected_gcsm,
       g.gcsm_event_time,g.gcsm_availability_time,g.gcsm_candidate_n,g.gcsm_interfered_n,g.gcsm_assessability_reason,
       w.wbc AS wpr_wbc,w.platelet AS wpr_platelet,w.wpr,w.log2_wpr,
       (w.subject_id IS NULL)::int AS wpr_unavailable,w.wpr_event_time,w.wpr_availability_time,w.specimen_id AS wpr_specimen_id
FROM cohort c LEFT JOIN pupil p USING(subject_id) JOIN gcs g USING(subject_id) LEFT JOIN wpr w USING(subject_id)
ORDER BY c.subject_id
) TO STDOUT WITH (FORMAT CSV, HEADER TRUE);

ROLLBACK;
