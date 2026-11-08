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
), windows AS MATERIALIZED (
  SELECT c.subject_id,c.hadm_id,c.episode_start,c.t0,j,
         greatest(c.episode_start,c.t0-(interval '24 hours'+j*interval '4 hours')) AS win_start,
         c.t0-j*interval '4 hours' AS win_end
  FROM cohort c CROSS JOIN generate_series(0,6) j
), labs AS MATERIALIZED (
  SELECT c.subject_id,le.itemid,le.charttime,greatest(le.charttime,le.storetime) AS availability_time,le.valuenum
  FROM cohort c CROSS JOIN LATERAL (
    SELECT itemid,charttime,storetime,valuenum
    FROM mimiciv_hosp.labevents le
    WHERE le.subject_id=c.subject_id AND le.hadm_id=c.hadm_id
      AND le.itemid IN (50885,50912,51265)
      AND le.charttime>=greatest(c.episode_start,c.t0-interval '48 hours') AND le.charttime<c.t0
      AND greatest(le.charttime,le.storetime)<=c.t0 AND le.valuenum>=0
  ) le
), lab_scores AS (
  SELECT w.subject_id,w.j,
    max(CASE WHEN l.itemid=50885 THEN CASE WHEN l.valuenum>12 THEN 4 WHEN l.valuenum>6 THEN 3 WHEN l.valuenum>3 THEN 2 WHEN l.valuenum>1.2 THEN 1 ELSE 0 END END) AS liver_score,
    max(CASE WHEN l.itemid=50912 THEN CASE WHEN l.valuenum>3.5 THEN 3 WHEN l.valuenum>2 THEN 2 WHEN l.valuenum>1.2 THEN 1 ELSE 0 END END) AS creatinine_score,
    max(CASE WHEN l.itemid=51265 THEN CASE WHEN l.valuenum<=50 THEN 4 WHEN l.valuenum<=80 THEN 3 WHEN l.valuenum<=100 THEN 2 WHEN l.valuenum<=150 THEN 1 ELSE 0 END END) AS hemostasis_score,
    count(*) FILTER (WHERE l.itemid=50885) AS liver_evidence_n,
    count(*) FILTER (WHERE l.itemid=50912) AS creatinine_evidence_n,
    count(*) FILTER (WHERE l.itemid=51265) AS hemostasis_evidence_n,
    max(l.charttime) FILTER (WHERE l.itemid=50885) AS liver_event_time,
    max(l.availability_time) FILTER (WHERE l.itemid=50885) AS liver_availability_time,
    max(l.charttime) FILTER (WHERE l.itemid=50912) AS creatinine_event_time,
    max(l.availability_time) FILTER (WHERE l.itemid=50912) AS creatinine_availability_time,
    max(l.charttime) FILTER (WHERE l.itemid=51265) AS hemostasis_event_time,
    max(l.availability_time) FILTER (WHERE l.itemid=51265) AS hemostasis_availability_time
  FROM windows w LEFT JOIN labs l ON l.subject_id=w.subject_id AND l.charttime>=w.win_start AND l.charttime<w.win_end
  GROUP BY w.subject_id,w.j
), map_events AS MATERIALIZED (
  SELECT s.subject_id,ce.charttime,greatest(ce.charttime,max(ce.storetime)) AS availability_time,
    CASE WHEN count(*) FILTER (WHERE ce.itemid IN (220052,225312))>0
         THEN avg(ce.valuenum) FILTER (WHERE ce.itemid IN (220052,225312))
         ELSE avg(ce.valuenum) FILTER (WHERE ce.itemid=220181) END AS map
  FROM stays s CROSS JOIN LATERAL (
    SELECT itemid,charttime,storetime,valuenum
    FROM mimiciv_icu.chartevents ce
    WHERE ce.stay_id=s.stay_id AND ce.itemid IN (220052,225312,220181)
      AND ce.charttime>=greatest(s.episode_start,s.t0-interval '48 hours') AND ce.charttime<s.t0
      AND greatest(ce.charttime,ce.storetime)<=s.t0 AND ce.valuenum>0 AND ce.valuenum<300
  ) ce GROUP BY s.subject_id,ce.charttime
), map_scores AS (
  SELECT w.subject_id,w.j,max(CASE WHEN m.map<70 THEN 1 ELSE 0 END) AS map_score,
         count(m.charttime) AS map_evidence_n,max(m.charttime) AS map_event_time,max(m.availability_time) AS map_availability_time
  FROM windows w LEFT JOIN map_events m ON m.subject_id=w.subject_id AND m.charttime>=w.win_start AND m.charttime<w.win_end
  GROUP BY w.subject_id,w.j
), vaso_source_patient AS MATERIALIZED (
  SELECT s.subject_id,max(e.storetime) FILTER (WHERE e.storetime<=s.t0) AS max_known_storetime,
         count(*) FILTER (WHERE e.storetime<=s.t0) AS known_n,
         count(*) FILTER (WHERE e.storetime>s.t0) AS delayed_n
  FROM stays s JOIN mimiciv_icu.inputevents e USING(stay_id)
  WHERE e.itemid IN (221289,229617,221662,221653,221906,221749,229630,229631,229632,222315,229709,229764)
    AND e.starttime<s.t0 AND e.endtime>greatest(s.episode_start,s.t0-interval '48 hours')
    AND coalesce(e.rate,e.amount,0)>0 AND e.statusdescription NOT IN ('Paused','Bolus')
  GROUP BY s.subject_id
), vaso_available AS MATERIALIZED (
  SELECT s.subject_id,v.*,src.max_known_storetime AS availability_time
  FROM stays s JOIN mimiciv_derived.vasoactive_agent v USING(stay_id)
  JOIN vaso_source_patient src USING(subject_id)
  WHERE v.starttime<s.t0 AND v.endtime>greatest(s.episode_start,s.t0-interval '48 hours')
    AND src.known_n>0 AND src.delayed_n=0
), vaso_scores AS (
  SELECT w.subject_id,w.j,
    max(CASE
      WHEN coalesce(v.norepinephrine,0)+coalesce(v.epinephrine,0)>0.4 THEN 4
      WHEN coalesce(v.norepinephrine,0)+coalesce(v.epinephrine,0)>0.2
           AND (coalesce(v.dopamine,0)>0 OR coalesce(v.phenylephrine,0)>0 OR coalesce(v.vasopressin,0)>0 OR coalesce(v.dobutamine,0)>0 OR coalesce(v.milrinone,0)>0) THEN 4
      WHEN coalesce(v.norepinephrine,0)+coalesce(v.epinephrine,0)>0.2 THEN 3
      WHEN coalesce(v.norepinephrine,0)+coalesce(v.epinephrine,0)>0
           AND (coalesce(v.dopamine,0)>0 OR coalesce(v.phenylephrine,0)>0 OR coalesce(v.vasopressin,0)>0 OR coalesce(v.dobutamine,0)>0 OR coalesce(v.milrinone,0)>0) THEN 3
      WHEN coalesce(v.norepinephrine,0)+coalesce(v.epinephrine,0)>0
        OR coalesce(v.dopamine,0)>0 OR coalesce(v.phenylephrine,0)>0 OR coalesce(v.vasopressin,0)>0 OR coalesce(v.dobutamine,0)>0 OR coalesce(v.milrinone,0)>0 THEN 2
      ELSE 0 END) AS vaso_score,
    count(v.starttime) AS vaso_evidence_n,max(v.endtime) AS vaso_event_time,max(v.availability_time) AS vaso_availability_time
  FROM windows w
  LEFT JOIN vaso_available v ON v.subject_id=w.subject_id AND v.starttime<w.win_end AND v.endtime>w.win_start
  GROUP BY w.subject_id,w.j
), mechanical_support AS MATERIALIZED (
  SELECT s.subject_id,ce.charttime,greatest(ce.charttime,ce.storetime) AS availability_time,
         CASE WHEN ce.itemid IN (224660,229270,229280,229278) THEN 'ECMO' ELSE 'MCS' END AS support_type
  FROM stays s CROSS JOIN LATERAL (
    SELECT itemid,charttime,storetime,valuenum
    FROM mimiciv_icu.chartevents ce
    WHERE ce.stay_id=s.stay_id AND ce.itemid IN (224660,229270,229280,229278,220125,220128,228154,229671)
      AND ce.charttime>=greatest(s.episode_start,s.t0-interval '48 hours') AND ce.charttime<s.t0
      AND greatest(ce.charttime,ce.storetime)<=s.t0 AND ce.valuenum>0
  ) ce
), mechanical_scores AS (
  SELECT w.subject_id,w.j,
    count(m.charttime) FILTER (WHERE m.support_type='MCS') AS mcs_evidence_n,
    count(m.charttime) FILTER (WHERE m.support_type='ECMO') AS ecmo_evidence_n,
    max(m.charttime) AS mechanical_event_time,max(m.availability_time) AS mechanical_availability_time
  FROM windows w LEFT JOIN mechanical_support m ON m.subject_id=w.subject_id AND m.charttime>=w.win_start AND m.charttime<w.win_end
  GROUP BY w.subject_id,w.j
), pao2 AS MATERIALIZED (
  SELECT c.subject_id,c.hadm_id,c.t0,le.labevent_id,le.specimen_id,le.charttime,
         greatest(le.charttime,le.storetime) AS po2_availability_time,le.valuenum AS pao2
  FROM cohort c CROSS JOIN LATERAL (
    SELECT labevent_id,specimen_id,charttime,storetime,valuenum
    FROM mimiciv_hosp.labevents le
    WHERE le.subject_id=c.subject_id AND le.hadm_id=c.hadm_id AND le.itemid=50821
      AND le.charttime>=greatest(c.episode_start,c.t0-interval '48 hours') AND le.charttime<c.t0
      AND le.valuenum>0 AND greatest(le.charttime,le.storetime)<=c.t0
  ) le
  WHERE EXISTS (
    SELECT 1 FROM mimiciv_hosp.labevents sp
    WHERE sp.specimen_id=le.specimen_id AND sp.itemid=52033 AND upper(trim(sp.value))='ART.'
      AND greatest(sp.charttime,sp.storetime)<=c.t0
  )
), fio2_lab AS MATERIALIZED (
  SELECT p.labevent_id,max(CASE WHEN f.valuenum>20 AND f.valuenum<=100 THEN f.valuenum
                                WHEN f.valuenum>0.2 AND f.valuenum<=1 THEN f.valuenum*100 END) AS fio2,
         max(greatest(f.charttime,f.storetime)) AS availability_time
  FROM pao2 p LEFT JOIN mimiciv_hosp.labevents f ON f.specimen_id=p.specimen_id AND f.itemid=50816
    AND greatest(f.charttime,f.storetime)<=p.t0
  GROUP BY p.labevent_id
), fio2_chart_candidates AS MATERIALIZED (
  SELECT p.labevent_id,ce.charttime,greatest(ce.charttime,ce.storetime) AS availability_time,
         CASE WHEN ce.valuenum>0.2 AND ce.valuenum<=1 THEN ce.valuenum*100
              WHEN ce.valuenum>=20 AND ce.valuenum<=100 THEN ce.valuenum END AS fio2,
         row_number() OVER (PARTITION BY p.labevent_id ORDER BY ce.charttime DESC,ce.storetime DESC) rn
  FROM pao2 p JOIN stays s ON s.subject_id=p.subject_id
  JOIN mimiciv_icu.chartevents ce ON ce.stay_id=s.stay_id AND ce.itemid=223835
    AND ce.charttime BETWEEN p.charttime-interval '4 hours' AND p.charttime
    AND greatest(ce.charttime,ce.storetime)<=p.t0
    AND ce.valuenum>0 AND ce.valuenum<=100
), fio2_chart AS (
  SELECT * FROM fio2_chart_candidates WHERE rn=1
), pf AS MATERIALIZED (
  SELECT p.subject_id,p.charttime,
         greatest(p.po2_availability_time,coalesce(fl.availability_time,fc.availability_time,p.po2_availability_time)) AS availability_time,
         100.0*p.pao2/nullif(coalesce(fl.fio2,fc.fio2),0) AS pf_ratio,
         EXISTS (
           SELECT 1 FROM stays s JOIN mimiciv_derived.ventilation v USING(stay_id)
           WHERE s.subject_id=p.subject_id AND v.ventilation_status IN ('InvasiveVent','NonInvasiveVent','HFNC')
             AND v.starttime<=p.charttime AND v.endtime>p.charttime
         ) AS advanced_support
  FROM pao2 p LEFT JOIN fio2_lab fl USING(labevent_id) LEFT JOIN fio2_chart fc USING(labevent_id)
  WHERE coalesce(fl.fio2,fc.fio2) IS NOT NULL
), respiratory_scores AS (
  SELECT w.subject_id,w.j,
    greatest(
      coalesce(max(CASE WHEN p.pf_ratio<=75 AND p.advanced_support THEN 4
                        WHEN p.pf_ratio<=150 AND p.advanced_support THEN 3
                        WHEN p.pf_ratio<=225 THEN 2 WHEN p.pf_ratio<=300 THEN 1 ELSE 0 END),0),
      CASE WHEN coalesce(ms.ecmo_evidence_n,0)>0 THEN 4 ELSE 0 END
    ) AS respiratory_score,
    count(p.charttime)+coalesce(ms.ecmo_evidence_n,0) AS respiratory_evidence_n,
    max(p.charttime) AS respiratory_event_time,max(p.availability_time) AS respiratory_availability_time
  FROM windows w LEFT JOIN pf p ON p.subject_id=w.subject_id AND p.charttime>=w.win_start AND p.charttime<w.win_end
  LEFT JOIN mechanical_scores ms ON ms.subject_id=w.subject_id AND ms.j=w.j
  GROUP BY w.subject_id,w.j,ms.ecmo_evidence_n
), urine_source_patient AS MATERIALIZED (
  SELECT s.subject_id,max(greatest(o.charttime,coalesce(o.storetime,o.charttime))) FILTER (
           WHERE greatest(o.charttime,coalesce(o.storetime,o.charttime))<=s.t0) AS max_known_storetime,
         count(*) FILTER (WHERE greatest(o.charttime,coalesce(o.storetime,o.charttime))<=s.t0) AS known_n,
         count(*) FILTER (WHERE greatest(o.charttime,coalesce(o.storetime,o.charttime))>s.t0) AS delayed_n
  FROM stays s JOIN mimiciv_icu.outputevents o USING(stay_id)
  WHERE o.itemid IN (226559,226560,226561,226584,226563,226564,226565,226567,226557,226558,227488,227489)
    AND o.charttime>=greatest(s.episode_start,s.t0-interval '72 hours') AND o.charttime<s.t0
  GROUP BY s.subject_id
), urine_available AS MATERIALIZED (
  SELECT s.subject_id,u.charttime,u.urineoutput_6hr,u.urineoutput_12hr,u.urineoutput_24hr,
         u.uo_mlkghr_6hr,u.uo_mlkghr_12hr,u.uo_mlkghr_24hr,u.uo_tm_6hr,u.uo_tm_12hr,u.uo_tm_24hr,
         src.max_known_storetime AS availability_time
  FROM stays s JOIN mimiciv_derived.urine_output_rate u ON u.stay_id=s.stay_id
  JOIN urine_source_patient src USING(subject_id)
  WHERE u.charttime>=greatest(s.episode_start,s.t0-interval '48 hours') AND u.charttime<s.t0
    AND src.delayed_n=0 AND src.known_n>0 AND src.max_known_storetime<=s.t0
), urine_scores AS (
  SELECT w.subject_id,w.j,
    max(CASE WHEN u.urineoutput_12hr=0 AND u.uo_tm_12hr>=12 THEN 3
             WHEN u.uo_mlkghr_24hr<0.3 AND u.uo_tm_24hr>=24 THEN 3
             WHEN u.uo_mlkghr_12hr<0.5 AND u.uo_tm_12hr>=12 THEN 2
             WHEN u.uo_mlkghr_6hr<0.5 AND u.uo_tm_6hr>=6 THEN 1 ELSE 0 END) AS urine_score,
    count(u.charttime) AS urine_evidence_n,max(u.charttime) AS urine_event_time,max(u.availability_time) AS urine_availability_time
  FROM windows w LEFT JOIN urine_available u ON u.subject_id=w.subject_id AND u.charttime>=w.win_start AND u.charttime<w.win_end
  GROUP BY w.subject_id,w.j
), rrt_evidence AS MATERIALIZED (
  SELECT s.subject_id,ce.charttime AS event_time,greatest(ce.charttime,ce.storetime) AS availability_time
  FROM stays s JOIN mimiciv_icu.chartevents ce USING(stay_id)
  WHERE (ce.itemid IN (226499,224154,225183,227438,224191,225806,225807,228004,228005,228006,224144,224145,224153,226457)
         OR (ce.itemid=225965 AND ce.value='In use'))
    AND ce.charttime>=greatest(s.episode_start,s.t0-interval '48 hours') AND ce.charttime<s.t0
    AND greatest(ce.charttime,ce.storetime)<=s.t0 AND ce.value IS NOT NULL
  UNION ALL
  SELECT s.subject_id,e.starttime,greatest(e.starttime,e.storetime)
  FROM stays s JOIN mimiciv_icu.inputevents e USING(stay_id)
  WHERE e.itemid IN (227536,227525) AND e.amount>0
    AND e.starttime<s.t0 AND e.endtime>greatest(s.episode_start,s.t0-interval '48 hours')
    AND greatest(e.starttime,e.storetime)<=s.t0
  UNION ALL
  SELECT s.subject_id,p.starttime,greatest(p.starttime,p.storetime)
  FROM stays s JOIN mimiciv_icu.procedureevents p USING(stay_id)
  WHERE p.itemid IN (225441,225802,225803,225805,225809,225955) AND p.value IS NOT NULL
    AND p.starttime<s.t0 AND p.endtime>greatest(s.episode_start,s.t0-interval '48 hours')
    AND greatest(p.starttime,p.storetime)<=s.t0
), rrt_scores AS (
  SELECT w.subject_id,w.j,(count(r.event_time)>0)::int AS rrt_active,
         count(r.event_time) AS rrt_evidence_n,max(r.event_time) AS rrt_event_time,max(r.availability_time) AS rrt_availability_time
  FROM windows w LEFT JOIN rrt_evidence r ON r.subject_id=w.subject_id AND r.event_time>=w.win_start AND r.event_time<w.win_end
  GROUP BY w.subject_id,w.j
), per_window AS (
  SELECT w.*,
    rs.respiratory_score,
    greatest(CASE WHEN coalesce(ms2.mcs_evidence_n,0)>0 THEN 4 ELSE 0 END,coalesce(vs.vaso_score,0),coalesce(maps.map_score,0)) AS cardiovascular_score,
    coalesce(ls.liver_score,0) AS liver_score,
    greatest(coalesce(ls.creatinine_score,0),coalesce(us.urine_score,0),CASE WHEN coalesce(rr.rrt_active,0)>0 THEN 4 ELSE 0 END) AS kidney_score,
    coalesce(ls.hemostasis_score,0) AS hemostasis_score,
    (coalesce(rs.respiratory_evidence_n,0)>0)::int AS respiratory_observed,
    ((coalesce(maps.map_evidence_n,0)+coalesce(vs.vaso_evidence_n,0)+coalesce(ms2.mcs_evidence_n,0))>0)::int AS cardiovascular_observed,
    (coalesce(ls.liver_evidence_n,0)>0)::int AS liver_observed,
    ((coalesce(ls.creatinine_evidence_n,0)+coalesce(us.urine_evidence_n,0)+coalesce(rr.rrt_evidence_n,0))>0)::int AS kidney_observed,
    (coalesce(ls.hemostasis_evidence_n,0)>0)::int AS hemostasis_observed,
    rs.respiratory_event_time,rs.respiratory_availability_time,
    greatest(maps.map_event_time,vs.vaso_event_time,ms2.mechanical_event_time) AS cardiovascular_event_time,
    greatest(maps.map_availability_time,vs.vaso_availability_time,ms2.mechanical_availability_time) AS cardiovascular_availability_time,
    ls.liver_event_time,ls.liver_availability_time,
    greatest(ls.creatinine_event_time,us.urine_event_time,rr.rrt_event_time) AS kidney_event_time,
    greatest(ls.creatinine_availability_time,us.urine_availability_time,rr.rrt_availability_time) AS kidney_availability_time,
    ls.hemostasis_event_time,ls.hemostasis_availability_time,
    coalesce(ms2.ecmo_evidence_n,0) AS ecmo_evidence_n,coalesce(ms2.mcs_evidence_n,0) AS mcs_evidence_n,
    coalesce(vs.vaso_evidence_n,0) AS vaso_evidence_n,coalesce(us.urine_evidence_n,0) AS urine_evidence_n,coalesce(rr.rrt_evidence_n,0) AS rrt_evidence_n
  FROM windows w
  JOIN lab_scores ls USING(subject_id,j) JOIN map_scores maps USING(subject_id,j)
  JOIN vaso_scores vs USING(subject_id,j) JOIN mechanical_scores ms2 USING(subject_id,j)
  JOIN respiratory_scores rs USING(subject_id,j) JOIN urine_scores us USING(subject_id,j) JOIN rrt_scores rr USING(subject_id,j)
), ranked AS (
  SELECT *,respiratory_observed+cardiovascular_observed+liver_observed+kidney_observed+hemostasis_observed AS observed_domain_n,
    row_number() OVER (PARTITION BY subject_id ORDER BY
      CASE WHEN respiratory_observed+cardiovascular_observed+liver_observed+kidney_observed+hemostasis_observed>0 THEN 0 ELSE 1 END,
      CASE WHEN respiratory_observed+cardiovascular_observed+liver_observed+kidney_observed+hemostasis_observed>0 THEN j ELSE -j END) AS selection_rank
  FROM per_window
), selected AS (
  SELECT * FROM ranked WHERE selection_rank=1
)
SELECT subject_id,hadm_id,j AS sofa_rollback_steps,win_start AS sofa_window_start,
       CASE WHEN observed_domain_n=0 THEN greatest(win_start,win_end) ELSE win_end END AS sofa_window_end,
       observed_domain_n,(observed_domain_n=0)::int AS all_domains_unobserved_48h,
       respiratory_score,cardiovascular_score,liver_score,kidney_score,hemostasis_score,
       respiratory_score+cardiovascular_score+liver_score+kidney_score+hemostasis_score AS modified_non_neuro_sofa2_total,
       ((respiratory_score>0)::int+(cardiovascular_score>0)::int+(liver_score>0)::int+(kidney_score>0)::int+(hemostasis_score>0)::int) AS abnormal_non_neuro_organ_count,
       respiratory_observed,cardiovascular_observed,liver_observed,kidney_observed,hemostasis_observed,
       respiratory_event_time,respiratory_availability_time,cardiovascular_event_time,cardiovascular_availability_time,
       liver_event_time,liver_availability_time,kidney_event_time,kidney_availability_time,
       hemostasis_event_time,hemostasis_availability_time,
       ecmo_evidence_n,mcs_evidence_n,vaso_evidence_n,urine_evidence_n,rrt_evidence_n
FROM selected ORDER BY subject_id
) TO STDOUT WITH (FORMAT CSV, HEADER TRUE);

ROLLBACK;
