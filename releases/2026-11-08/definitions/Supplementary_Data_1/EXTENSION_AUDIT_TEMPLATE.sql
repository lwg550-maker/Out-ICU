SET default_transaction_read_only=on;
BEGIN READ ONLY;
SHOW transaction_read_only;
SET LOCAL statement_timeout='30min';
SET LOCAL enable_hashjoin=off;
WITH cohort(subject_id,hadm_id,episode_start,t0) AS MATERIALIZED (
  VALUES __COHORT_VALUES__
), expected_contracts(variable_id,source_table,itemid,event_time_field,availability_time_field,
                       unit_rule,contract_role,min_value,max_value,window_rule,source_contract_id) AS MATERIALIZED (
  VALUES __EXPECTED_CONTRACT_VALUES__
), stays AS MATERIALIZED (
  SELECT c.subject_id,c.hadm_id,c.episode_start,c.t0,i.stay_id
  FROM cohort c JOIN mimiciv_icu.icustays i USING(subject_id,hadm_id)
  WHERE i.intime<c.t0 AND coalesce(i.outtime,c.t0)>c.episode_start
), ce_map AS MATERIALIZED (
  SELECT * FROM expected_contracts
  WHERE source_table='mimiciv_icu.chartevents'
    AND variable_id NOT IN ('invasive_vent_active_t0','hours_since_invasive_vent_end')
), ce_source AS MATERIALIZED (
  SELECT m.variable_id,m.source_contract_id,m.source_table,m.itemid,coalesce(ce.valueuom,'') AS source_unit,
         ce.subject_id,s.hadm_id,s.episode_start,s.t0,ce.charttime AS event_time,ce.storetime,
         greatest(ce.charttime,ce.storetime) AS availability_time,NULL::timestamp AS endtime,
         (ce.storetime IS NOT NULL
          AND ce.charttime>=s.episode_start AND ce.charttime<s.t0
          AND (m.window_rule<>'initial6h' OR ce.charttime<least(s.episode_start+interval '6 hours',s.t0))
          AND (m.window_rule<>'current4h' OR greatest(ce.charttime,ce.storetime)>=s.t0-interval '4 hours')
          AND greatest(ce.charttime,ce.storetime)<=s.t0
          AND coalesce(CASE
            WHEN m.unit_rule IN ('categorical','pupil_category') THEN ce.value IS NOT NULL
            WHEN m.unit_rule IN ('itemid_score','itemid223835_percent') THEN ce.valuenum IS NOT NULL
            WHEN m.unit_rule='temperature_c' THEN ce.valueuom IN ('°C','Deg. C','C')
            WHEN m.unit_rule='temperature_f' THEN ce.valueuom IN ('°F','Deg. F','F')
            WHEN m.unit_rule='percent' THEN ce.valueuom IN ('%','')
            WHEN m.unit_rule='binary_activity' THEN (ce.valuenum>0 OR nullif(trim(ce.value),'') IS NOT NULL)
            ELSE ce.valueuom=m.unit_rule END,false)
          AND (m.min_value IS NULL OR ce.valuenum>=m.min_value)
          AND (m.max_value IS NULL OR ce.valuenum<=m.max_value)) AS included,
         CASE
           WHEN ce.storetime IS NULL THEN 'MISSING_STORETIME'
           WHEN greatest(ce.charttime,ce.storetime)>s.t0 THEN 'AVAILABLE_AFTER_T0'
           WHEN m.window_rule='initial6h' AND ce.charttime>=least(s.episode_start+interval '6 hours',s.t0) THEN 'OUTSIDE_INITIAL_6H'
           WHEN m.window_rule='current4h' AND greatest(ce.charttime,ce.storetime)<s.t0-interval '4 hours' THEN 'STALE_CURRENT_STATE'
           WHEN NOT coalesce(CASE
             WHEN m.unit_rule IN ('categorical','pupil_category') THEN ce.value IS NOT NULL
             WHEN m.unit_rule IN ('itemid_score','itemid223835_percent') THEN ce.valuenum IS NOT NULL
             WHEN m.unit_rule='temperature_c' THEN ce.valueuom IN ('°C','Deg. C','C')
             WHEN m.unit_rule='temperature_f' THEN ce.valueuom IN ('°F','Deg. F','F')
             WHEN m.unit_rule='percent' THEN ce.valueuom IN ('%','')
             WHEN m.unit_rule='binary_activity' THEN (ce.valuenum>0 OR nullif(trim(ce.value),'') IS NOT NULL)
             ELSE ce.valueuom=m.unit_rule END,false) THEN 'UNKNOWN_OR_INVALID_UNIT'
           WHEN (m.min_value IS NOT NULL AND ce.valuenum<m.min_value)
             OR (m.max_value IS NOT NULL AND ce.valuenum>m.max_value) THEN 'OUTSIDE_LEGAL_RANGE'
           ELSE 'OK' END AS anomaly_code,false AS future_endtime_used
  FROM stays s JOIN mimiciv_icu.chartevents ce USING(stay_id) JOIN ce_map m USING(itemid)
  WHERE ce.charttime>=s.episode_start AND ce.charttime<s.t0
), lab_map AS MATERIALIZED (
  SELECT * FROM expected_contracts WHERE source_table='mimiciv_hosp.labevents'
), lab_ranked AS MATERIALIZED (
  SELECT m.*,le.subject_id,c.hadm_id,c.episode_start,c.t0,le.labevent_id,le.specimen_id,
         le.charttime,le.storetime,le.valuenum,coalesce(le.valueuom,'') AS valueuom,
         row_number() OVER (PARTITION BY le.subject_id,le.hadm_id,m.variable_id,le.specimen_id,le.charttime
                            ORDER BY le.storetime DESC NULLS LAST,le.labevent_id DESC) AS correction_rank
  FROM cohort c JOIN mimiciv_hosp.labevents le USING(subject_id,hadm_id) JOIN lab_map m USING(itemid)
  WHERE le.charttime>=c.episode_start AND le.charttime<c.t0
), lab_source AS MATERIALIZED (
  SELECT variable_id,source_contract_id,source_table,itemid,valueuom,subject_id,hadm_id,episode_start,t0,
         charttime,storetime,greatest(charttime,storetime),NULL::timestamp,
         (correction_rank=1 AND storetime IS NOT NULL AND valuenum IS NOT NULL AND greatest(charttime,storetime)<=t0
          AND CASE WHEN unit_rule='mEq/L_or_mmol/L' THEN valueuom IN ('mEq/L','mmol/L')
                   WHEN unit_rule='ratio' THEN valueuom IN ('','ratio') ELSE valueuom=unit_rule END
          AND valuenum>=min_value AND valuenum<=max_value),
         CASE WHEN correction_rank>1 THEN 'SUPERSEDED_SAME_SPECIMEN_RESULT'
              WHEN storetime IS NULL THEN 'MISSING_STORETIME'
              WHEN valuenum IS NULL THEN 'MISSING_VALUE'
              WHEN greatest(charttime,storetime)>t0 THEN 'AVAILABLE_AFTER_T0'
              WHEN NOT CASE WHEN unit_rule='mEq/L_or_mmol/L' THEN valueuom IN ('mEq/L','mmol/L')
                            WHEN unit_rule='ratio' THEN valueuom IN ('','ratio') ELSE valueuom=unit_rule END
                THEN 'UNKNOWN_OR_INVALID_UNIT'
              WHEN valuenum<min_value OR valuenum>max_value THEN 'OUTSIDE_LEGAL_RANGE' ELSE 'OK' END,
         false
  FROM lab_ranked
), input_observed AS MATERIALIZED (
  SELECT m.*,e.subject_id,c.hadm_id,c.episode_start,c.t0,e.orderid,e.starttime,e.endtime,e.storetime,
         e.rate,e.rateuom,e.amount,e.amountuom,e.statusdescription
  FROM cohort c JOIN mimiciv_icu.inputevents e ON e.hadm_id=c.hadm_id
  JOIN expected_contracts m ON m.source_table='mimiciv_icu.inputevents' AND m.itemid=e.itemid
  WHERE e.starttime<c.t0 AND coalesce(e.endtime,e.starttime)>=c.episode_start
), input_visible AS MATERIALIZED (
  SELECT m.*,e.subject_id,c.hadm_id,c.episode_start,c.t0,e.orderid,e.starttime,e.endtime,e.storetime,
         e.rate,e.rateuom,e.amount,e.amountuom,e.statusdescription,greatest(e.starttime,e.storetime) AS state_time,
         (e.rate>0 AND lower(coalesce(e.statusdescription,'')) NOT IN
           ('paused','rewritten','stopped','finishedrunning','completed','discontinued')) AS positive_nonstop
  FROM cohort c JOIN mimiciv_icu.inputevents e ON e.hadm_id=c.hadm_id
  JOIN expected_contracts m ON m.source_table='mimiciv_icu.inputevents' AND m.itemid=e.itemid
  WHERE m.contract_role='current4h' AND e.starttime<c.t0 AND e.storetime IS NOT NULL AND e.storetime<=c.t0
    AND coalesce(e.endtime,e.starttime)>=c.episode_start
), input_ranked AS MATERIALIZED (
  SELECT *,row_number() OVER (
    PARTITION BY subject_id,hadm_id,variable_id,itemid
    ORDER BY state_time DESC,orderid DESC
  ) AS state_rank
  FROM input_visible
), input_source AS MATERIALIZED (
  SELECT o.variable_id,o.source_contract_id,o.source_table,o.itemid,
         CASE WHEN o.contract_role='completed_exposure' THEN coalesce(o.amountuom,'') ELSE coalesce(o.rateuom,'') END,
         o.subject_id,o.hadm_id,o.episode_start,o.t0,
         CASE WHEN o.contract_role IN ('completed_stop','completed_exposure') THEN o.endtime ELSE o.starttime END,
         o.storetime,
         CASE WHEN o.contract_role IN ('completed_stop','completed_exposure') THEN greatest(o.endtime,o.storetime)
              ELSE greatest(o.starttime,o.storetime) END,
         o.endtime,
         CASE WHEN o.contract_role='current4h' THEN r.state_rank=1 AND r.positive_nonstop
                    AND r.state_time>=o.t0-interval '4 hours'
              WHEN o.contract_role='completed_stop' THEN o.endtime IS NOT NULL AND o.storetime IS NOT NULL
                    AND o.endtime<o.t0 AND greatest(o.endtime,o.storetime)<=o.t0
              ELSE o.endtime IS NOT NULL AND o.storetime IS NOT NULL AND o.endtime<o.t0
                    AND greatest(o.endtime,o.storetime)<=o.t0 AND coalesce(o.amount,o.rate,0)>0
                    AND lower(coalesce(o.statusdescription,'')) NOT IN ('paused','rewritten','stopped','discontinued') END,
         CASE WHEN o.storetime IS NULL THEN 'MISSING_STORETIME'
              WHEN o.storetime>o.t0 THEN 'AVAILABLE_AFTER_T0'
              WHEN o.contract_role IN ('completed_stop','completed_exposure') AND o.endtime IS NULL THEN 'MISSING_COMPLETION_TIME'
              WHEN o.contract_role IN ('completed_stop','completed_exposure') AND greatest(o.endtime,o.storetime)>o.t0 THEN 'AVAILABLE_AFTER_T0'
              WHEN o.contract_role='current4h' AND r.state_rank>1 THEN 'SUPERSEDED_VISIBLE_STATE'
              WHEN o.contract_role='current4h' AND r.state_rank=1 AND NOT r.positive_nonstop THEN 'EXPLICIT_NONACTIVE_STATE'
              WHEN o.contract_role='current4h' AND r.state_rank=1 AND r.state_time<o.t0-interval '4 hours' THEN 'STALE_CURRENT_STATE'
              WHEN o.contract_role='current4h' AND o.endtime>o.t0 THEN 'FUTURE_END_PRESENT_NOT_USED'
              ELSE 'OK' END,
         false
  FROM input_observed o LEFT JOIN input_ranked r
    ON r.subject_id=o.subject_id AND r.hadm_id=o.hadm_id AND r.variable_id=o.variable_id
   AND r.itemid=o.itemid AND r.orderid=o.orderid
), proc_observed AS MATERIALIZED (
  SELECT m.*,p.subject_id,c.hadm_id,c.episode_start,c.t0,p.orderid,p.starttime,p.endtime,p.storetime,p.statusdescription
  FROM cohort c JOIN mimiciv_icu.procedureevents p ON p.hadm_id=c.hadm_id
  JOIN expected_contracts m ON m.source_table='mimiciv_icu.procedureevents' AND m.itemid=p.itemid
  WHERE p.starttime<c.t0 AND coalesce(p.endtime,p.starttime)>=c.episode_start
), proc_visible AS MATERIALIZED (
  SELECT *,greatest(starttime,storetime) AS state_time
  FROM proc_observed
  WHERE contract_role='procedure_current' AND storetime IS NOT NULL AND storetime<=t0
), proc_ranked AS MATERIALIZED (
  SELECT *,row_number() OVER (PARTITION BY subject_id,hadm_id,variable_id,itemid
                              ORDER BY state_time DESC,orderid DESC) AS state_rank
  FROM proc_visible
), proc_source AS MATERIALIZED (
  SELECT o.variable_id,o.source_contract_id,o.source_table,o.itemid,''::text,o.subject_id,o.hadm_id,
         o.episode_start,o.t0,CASE WHEN o.contract_role='procedure_current' THEN o.starttime ELSE o.endtime END,
         o.storetime,CASE WHEN o.contract_role='procedure_current' THEN greatest(o.starttime,o.storetime)
                          ELSE greatest(o.endtime,o.storetime) END,o.endtime,
         CASE WHEN o.contract_role='procedure_current' THEN r.state_rank=1
                    AND r.state_time>=o.t0-interval '4 hours'
                    AND lower(coalesce(r.statusdescription,'')) NOT IN ('stopped','finishedrunning','completed','discontinued')
              ELSE o.endtime IS NOT NULL AND o.storetime IS NOT NULL AND o.endtime<o.t0
                    AND greatest(o.endtime,o.storetime)<=o.t0 END,
         CASE WHEN o.storetime IS NULL THEN 'MISSING_STORETIME'
              WHEN o.storetime>o.t0 THEN 'AVAILABLE_AFTER_T0'
              WHEN o.contract_role='procedure_current' AND r.state_rank>1 THEN 'SUPERSEDED_VISIBLE_STATE'
              WHEN o.contract_role='procedure_current' AND r.state_rank=1 AND r.state_time<o.t0-interval '4 hours' THEN 'STALE_CURRENT_STATE'
              WHEN o.contract_role='procedure_current' AND o.endtime>o.t0 THEN 'FUTURE_END_PRESENT_NOT_USED'
              WHEN o.contract_role='procedure_completed' AND o.endtime IS NULL THEN 'MISSING_COMPLETION_TIME'
              WHEN o.contract_role='procedure_completed' AND greatest(o.endtime,o.storetime)>o.t0 THEN 'AVAILABLE_AFTER_T0'
              ELSE 'OK' END,false
  FROM proc_observed o LEFT JOIN proc_ranked r
    ON r.subject_id=o.subject_id AND r.hadm_id=o.hadm_id AND r.variable_id=o.variable_id
   AND r.itemid=o.itemid AND r.orderid=o.orderid
), vent_contracts AS MATERIALIZED (
  SELECT * FROM expected_contracts
  WHERE variable_id IN ('invasive_vent_active_t0','hours_since_invasive_vent_end')
), vent_observed AS MATERIALIZED (
  SELECT m.*,ce.subject_id,s.hadm_id,s.stay_id,s.episode_start,s.t0,ce.charttime,ce.storetime,ce.value,ce.valuenum,
         coalesce(ce.valueuom,'') AS valueuom
  FROM stays s JOIN mimiciv_icu.chartevents ce USING(stay_id) JOIN vent_contracts m USING(itemid)
  WHERE ce.charttime>=s.episode_start AND ce.charttime<s.t0
), vent_evidence_state AS MATERIALIZED (
  SELECT s.subject_id,s.hadm_id,s.stay_id,s.episode_start,s.t0,ce.charttime,max(ce.storetime) AS availability_time,
         CASE
           WHEN bool_or(ce.itemid=223849 AND lower(trim(ce.value))='standby') THEN 'nonactive'
           WHEN bool_or(ce.itemid IN (223849,229314) AND ce.value IN (
             '(S) CMV','APRV','APRV/Biphasic+ApnPress','APRV/Biphasic+ApnVol','APV (cmv)',
             'Ambient','Apnea Ventilation','CMV','CMV/ASSIST','CMV/ASSIST/AutoFlow','CMV/AutoFlow',
             'CPAP/PPS','CPAP/PSV','CPAP/PSV+Apn TCPL','CPAP/PSV+ApnPres','CPAP/PSV+ApnVol',
             'MMV','MMV/AutoFlow','MMV/PSV','MMV/PSV/AutoFlow','P-CMV','PCV+','PCV+/PSV',
             'PCV+Assist','PRES/AC','PRVC/AC','PRVC/SIMV','PSV/SBT','SIMV','SIMV/AutoFlow',
             'SIMV/PRES','SIMV/PSV','SIMV/PSV/AutoFlow','SIMV/VOL','SYNCHRON MASTER',
             'SYNCHRON SLAVE','VOL/AC','APV (simv)','P-SIMV','VS','ASV'))
             OR bool_or(ce.itemid IN (224688,224684,224685,224686,224696,220339,224700) AND ce.valuenum>0)
             THEN 'active'
           WHEN bool_or(ce.itemid=226732 AND trim(ce.value) IN ('Tracheostomy tube','Trach mask','T-piece'))
             THEN 'nonactive'
           ELSE 'other' END AS evidence_state
  FROM stays s JOIN mimiciv_icu.chartevents ce USING(stay_id)
  WHERE ce.itemid IN (223849,229314,224688,224684,224685,224686,224696,220339,224700,226732)
    AND ce.charttime>=s.episode_start AND ce.charttime<s.t0
    AND ce.storetime IS NOT NULL AND greatest(ce.charttime,ce.storetime)<=s.t0
  GROUP BY s.subject_id,s.hadm_id,s.stay_id,s.episode_start,s.t0,ce.charttime
), vent_candidates AS MATERIALIZED (
  SELECT s.subject_id,s.hadm_id,s.stay_id,s.t0,greatest(v.starttime,s.episode_start) AS candidate_start,
         v.endtime AS candidate_end
  FROM stays s JOIN mimiciv_derived.ventilation v USING(stay_id)
  WHERE v.ventilation_status IN ('InvasiveVent','Tracheostomy')
    AND v.starttime<s.t0 AND v.endtime>s.episode_start
), vent_ordered AS MATERIALIZED (
  SELECT *,min(charttime) FILTER (WHERE evidence_state='nonactive') OVER (
    PARTITION BY subject_id,hadm_id,stay_id ORDER BY charttime
    ROWS BETWEEN 1 FOLLOWING AND UNBOUNDED FOLLOWING
  ) AS next_nonactive
  FROM vent_evidence_state
), vent_active_seed AS MATERIALIZED (
  SELECT o.subject_id,o.hadm_id,o.stay_id,o.t0,o.charttime AS evidence_time,o.availability_time,
         greatest(o.charttime,c.candidate_start) AS interval_start,
         least(o.charttime+interval '14 hours',coalesce(o.next_nonactive,o.charttime+interval '14 hours'),c.candidate_end) AS raw_interval_end
  FROM vent_ordered o JOIN vent_candidates c USING(subject_id,hadm_id,stay_id,t0)
  WHERE o.evidence_state='active' AND o.charttime<c.candidate_end
    AND o.charttime+interval '14 hours'>c.candidate_start
), vent_interval_end AS MATERIALIZED (
  SELECT * FROM vent_active_seed WHERE raw_interval_end>interval_start
), vent_marked AS MATERIALIZED (
  SELECT *,max(raw_interval_end) OVER (PARTITION BY subject_id,hadm_id ORDER BY interval_start,raw_interval_end
    ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS previous_max_end
  FROM vent_interval_end
), vent_grouped AS MATERIALIZED (
  SELECT *,sum(CASE WHEN previous_max_end IS NULL OR interval_start>previous_max_end THEN 1 ELSE 0 END)
    OVER (PARTITION BY subject_id,hadm_id ORDER BY interval_start,raw_interval_end) AS interval_group
  FROM vent_marked
), vent_islands AS MATERIALIZED (
  SELECT subject_id,hadm_id,t0,interval_group,min(interval_start) AS interval_start,
         max(raw_interval_end) AS interval_end,max(availability_time) AS source_availability_time,
         max(evidence_time) AS last_evidence_time
  FROM vent_grouped GROUP BY subject_id,hadm_id,t0,interval_group
), vent_patient_state AS MATERIALIZED (
  SELECT subject_id,hadm_id,t0,bool_or(interval_start<=t0 AND t0<interval_end) AS active_t0,
         max(interval_end) FILTER (WHERE interval_end<=t0) AS last_interval_end,
         max(source_availability_time) FILTER (WHERE interval_end<=t0) AS last_interval_availability_time,
         max(last_evidence_time) FILTER (WHERE interval_start<=t0 AND t0<interval_end) AS active_evidence_time,
         max(source_availability_time) FILTER (WHERE interval_start<=t0 AND t0<interval_end) AS active_availability_time
  FROM vent_islands GROUP BY subject_id,hadm_id,t0
), vent_source AS MATERIALIZED (
  SELECT o.variable_id,o.source_contract_id,o.source_table,o.itemid,o.valueuom,o.subject_id,o.hadm_id,
         o.episode_start,o.t0,
         CASE WHEN o.variable_id='invasive_vent_active_t0' THEN coalesce(ps.active_evidence_time,o.charttime)
              ELSE coalesce(ps.last_interval_end,o.charttime) END,
         o.storetime,
         CASE WHEN o.variable_id='invasive_vent_active_t0' THEN coalesce(ps.active_availability_time,greatest(o.charttime,o.storetime))
              ELSE coalesce(greatest(ps.last_interval_end,ps.last_interval_availability_time),
                            greatest(o.charttime,o.storetime)) END,
         NULL::timestamp,
         (o.storetime IS NOT NULL AND greatest(o.charttime,o.storetime)<=o.t0
          AND es.evidence_state IN ('active','nonactive')
          AND CASE WHEN o.variable_id='invasive_vent_active_t0' THEN coalesce(ps.active_t0,false)
                   ELSE NOT coalesce(ps.active_t0,false) AND ps.last_interval_end IS NOT NULL END),
         CASE WHEN o.storetime IS NULL THEN 'MISSING_STORETIME'
              WHEN greatest(o.charttime,o.storetime)>o.t0 THEN 'AVAILABLE_AFTER_T0'
              WHEN es.evidence_state IS NULL OR es.evidence_state='other' THEN 'UNCLASSIFIED_VENT_EVIDENCE'
              WHEN o.variable_id='invasive_vent_active_t0' AND NOT coalesce(ps.active_t0,false) THEN 'NO_ACTIVE_INTERVAL_AT_T0'
              WHEN o.variable_id='hours_since_invasive_vent_end' AND ps.last_interval_end IS NULL THEN 'NO_CONFIRMED_INTERVAL_END'
              ELSE 'OK' END,false
  FROM vent_observed o
  LEFT JOIN vent_evidence_state es USING(subject_id,hadm_id,stay_id,episode_start,t0,charttime)
  LEFT JOIN vent_patient_state ps USING(subject_id,hadm_id,t0)
), raw_precheck(variable_id,source_contract_id,source_table,itemid,source_unit,subject_id,hadm_id,episode_start,t0,
       event_time,storetime,availability_time,endtime,included,anomaly_code,future_endtime_used) AS MATERIALIZED (
  SELECT * FROM ce_source UNION ALL SELECT * FROM lab_source UNION ALL SELECT * FROM input_source
  UNION ALL SELECT * FROM proc_source UNION ALL SELECT * FROM vent_source
), raw AS MATERIALIZED (
  SELECT variable_id,source_contract_id,source_table,itemid,source_unit,subject_id,hadm_id,episode_start,t0,
         event_time,storetime,availability_time,endtime,included,
         CASE WHEN included AND (event_time IS NULL OR availability_time IS NULL OR availability_time<event_time)
              THEN 'AVAILABILITY_BEFORE_EVENT' ELSE anomaly_code END AS anomaly_code,
         future_endtime_used
  FROM raw_precheck
), contracted AS MATERIALIZED (
  SELECT *,row_number() OVER (PARTITION BY source_contract_id ORDER BY
    md5('20261023|'||subject_id::text||'|'||hadm_id::text||'|'||itemid::text||'|'||event_time::text||'|'||coalesce(availability_time::text,''))
  ) AS sample_rank
  FROM raw
), actual_summaries AS MATERIALIZED (
  SELECT source_contract_id,count(*)::bigint AS record_count,
         count(*) FILTER (WHERE included)::bigint AS included_count,
         count(*) FILTER (WHERE anomaly_code<>'OK')::bigint AS anomaly_count
  FROM contracted GROUP BY source_contract_id
), summaries AS (
  SELECT 'SUMMARY'::text AS record_kind,e.variable_id,e.source_contract_id,e.source_table,e.itemid,
         e.unit_rule AS source_unit,NULL::bigint AS subject_id,NULL::bigint AS hadm_id,
         NULL::timestamp AS episode_start,NULL::timestamp AS t0,NULL::timestamp AS event_time,
         NULL::timestamp AS storetime,NULL::timestamp AS availability_time,NULL::timestamp AS endtime,
         NULL::boolean AS included,'SUMMARY'::text AS anomaly_code,NULL::boolean AS future_endtime_used,
         coalesce(a.record_count,0)::bigint AS record_count,
         coalesce(a.included_count,0)::bigint AS included_count,
         coalesce(a.anomaly_count,0)::bigint AS anomaly_count
  FROM expected_contracts e LEFT JOIN actual_summaries a USING(source_contract_id)
), samples AS (
  SELECT 'SAMPLE'::text,variable_id,source_contract_id,source_table,itemid,source_unit,subject_id,hadm_id,
         episode_start,t0,event_time,storetime,availability_time,endtime,included,anomaly_code,future_endtime_used,
         1::bigint,0::bigint,0::bigint
  FROM contracted WHERE sample_rank<=100
), boundaries AS (
  SELECT 'BOUNDARY'::text,variable_id,source_contract_id,source_table,itemid,source_unit,subject_id,hadm_id,
         episode_start,t0,event_time,storetime,availability_time,endtime,included,anomaly_code,future_endtime_used,
         1::bigint,0::bigint,1::bigint
  FROM contracted WHERE anomaly_code<>'OK'
)
SELECT record_kind,variable_id,source_contract_id,source_table,itemid,source_unit,subject_id,hadm_id,
       episode_start,t0,event_time,storetime,availability_time,endtime,included,anomaly_code,future_endtime_used,
       record_count,included_count,anomaly_count
FROM summaries
UNION ALL SELECT * FROM samples
UNION ALL SELECT * FROM boundaries
ORDER BY record_kind,variable_id,source_contract_id,subject_id,event_time;
ROLLBACK;
