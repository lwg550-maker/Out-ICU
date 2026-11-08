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
), initial_gcs_times AS MATERIALIZED (
  SELECT DISTINCT s.subject_id,s.hadm_id,s.stay_id,s.episode_start,s.t0,ce.charttime
  FROM stays s JOIN mimiciv_icu.chartevents ce USING(stay_id)
  WHERE ce.itemid IN (220739,223900,223901)
    AND ce.charttime>=s.episode_start
    AND ce.charttime<least(s.episode_start+interval '6 hours',s.t0)
), initial_gcs_input_events AS MATERIALIZED (
  SELECT c.subject_id,c.hadm_id,c.episode_start,c.t0,e.itemid,e.starttime,e.endtime,
         e.storetime,e.rate,e.statusdescription
  FROM cohort c JOIN mimiciv_icu.inputevents e ON e.hadm_id=c.hadm_id
  WHERE e.itemid IN (221555,222062,229233,221668,222168,225150,229420)
    AND e.starttime<least(c.episode_start+interval '6 hours',c.t0)
    AND coalesce(e.endtime,e.starttime)>c.episode_start
    AND e.storetime IS NOT NULL AND e.storetime<=c.t0 AND e.rate>0
    AND lower(coalesce(e.statusdescription,'')) NOT IN
      ('paused','rewritten','stopped','finishedrunning','completed','discontinued')
), initial_gcs_input_state AS MATERIALIZED (
  SELECT g.subject_id,g.hadm_id,g.stay_id,g.episode_start,g.t0,g.charttime,
         coalesce(bool_or(e.itemid IN (221555,222062,229233)),false) AS nmb_active,
         coalesce(bool_or(e.itemid IN (221668,222168,225150,229420)),false) AS sedative_active
  FROM initial_gcs_times g
  LEFT JOIN initial_gcs_input_events e
    ON e.subject_id=g.subject_id AND e.hadm_id=g.hadm_id AND e.episode_start=g.episode_start AND e.t0=g.t0
   AND e.starttime<=g.charttime AND coalesce(e.endtime,e.starttime)>g.charttime
  GROUP BY g.subject_id,g.hadm_id,g.stay_id,g.episode_start,g.t0,g.charttime
), initial_gcs_rass_ranked AS MATERIALIZED (
  SELECT g.subject_id,g.hadm_id,g.stay_id,g.episode_start,g.t0,g.charttime,r.valuenum,
         row_number() OVER (
           PARTITION BY g.subject_id,g.hadm_id,g.stay_id,g.episode_start,g.t0,g.charttime
           ORDER BY r.storetime DESC,r.valuenum ASC NULLS LAST
         ) AS rass_rank
  FROM initial_gcs_times g
  JOIN mimiciv_icu.chartevents r
    ON r.stay_id=g.stay_id AND r.itemid=228096 AND r.charttime=g.charttime
   AND r.storetime IS NOT NULL AND greatest(r.charttime,r.storetime)<=g.t0
), initial_gcs_rass_state AS MATERIALIZED (
  SELECT subject_id,hadm_id,stay_id,episode_start,t0,charttime,valuenum AS rass
  FROM initial_gcs_rass_ranked WHERE rass_rank=1
), initial_gcs_interference AS MATERIALIZED (
  SELECT g.*,coalesce(i.nmb_active,false) AS nmb_active,
         coalesce(i.sedative_active,false) AS sedative_active,r.rass
  FROM initial_gcs_times g
  LEFT JOIN initial_gcs_input_state i
    USING(subject_id,hadm_id,stay_id,episode_start,t0,charttime)
  LEFT JOIN initial_gcs_rass_state r
    USING(subject_id,hadm_id,stay_id,episode_start,t0,charttime)
), ce_source AS MATERIALIZED (
  SELECT m.variable_id,m.source_contract_id,m.source_table,m.itemid,coalesce(ce.valueuom,'') AS source_unit,
         ce.subject_id,s.hadm_id,s.episode_start,s.t0,ce.charttime AS event_time,ce.storetime,
         greatest(ce.charttime,ce.storetime) AS availability_time,NULL::timestamp AS endtime,
         coalesce((ce.storetime IS NOT NULL
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
            WHEN m.unit_rule='binary_activity' THEN
              (coalesce(ce.valuenum>0,false) OR nullif(trim(ce.value),'') IS NOT NULL)
            ELSE ce.valueuom=m.unit_rule END,false)
          AND (m.min_value IS NULL OR ce.valuenum>=m.min_value)
          AND (m.max_value IS NULL OR ce.valuenum<=m.max_value)),false) AS time_included,
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
             WHEN m.unit_rule='binary_activity' THEN
               (coalesce(ce.valuenum>0,false) OR nullif(trim(ce.value),'') IS NOT NULL)
             ELSE ce.valueuom=m.unit_rule END,false) THEN 'UNKNOWN_OR_INVALID_UNIT'
           WHEN (m.min_value IS NOT NULL AND ce.valuenum<m.min_value)
             OR (m.max_value IS NOT NULL AND ce.valuenum>m.max_value) THEN 'OUTSIDE_LEGAL_RANGE'
           ELSE 'OK' END AS anomaly_code,false AS future_endtime_used,
         NULL::text AS native_labevent_id,
         concat_ws(chr(31),ce.subject_id::text,s.hadm_id::text,ce.stay_id::text,ce.itemid::text,
           to_char(ce.charttime,'YYYY-MM-DD"T"HH24:MI:SS.US'),
           coalesce(to_char(ce.storetime,'YYYY-MM-DD"T"HH24:MI:SS.US'),chr(92)||'N'),
           coalesce(ce.caregiver_id::text,chr(92)||'N'),coalesce(ce.value,chr(92)||'N'),
           coalesce(CASE WHEN ce.valuenum::numeric=0 THEN '0' ELSE
             regexp_replace(regexp_replace((ce.valuenum::numeric)::text,'(\.\d*?)0+$','\1'),'\.$','') END,chr(92)||'N'),
           coalesce(ce.valueuom,chr(92)||'N'),coalesce(ce.warning::text,chr(92)||'N')) AS key_payload,
         CASE WHEN m.unit_rule='temperature_f' THEN (ce.valuenum-32.0)*5.0/9.0
              WHEN m.unit_rule='itemid223835_percent' THEN ce.valuenum/100.0
              ELSE ce.valuenum END AS raw_numeric_value,
         ce.value AS raw_text_value,NULL::numeric AS rate,NULL::numeric AS amount,
         CASE WHEN m.variable_id='initial_gcs_total' THEN
           'nmb_active='||CASE WHEN coalesce(gi.nmb_active,false) THEN '1' ELSE '0' END||
           '|sedative_active='||CASE WHEN coalesce(gi.sedative_active,false) THEN '1' ELSE '0' END||
           '|rass='||coalesce(gi.rass::text,'NA') ELSE NULL::text END AS status_text
  FROM stays s JOIN mimiciv_icu.chartevents ce USING(stay_id) JOIN ce_map m USING(itemid)
  LEFT JOIN initial_gcs_interference gi
    ON gi.subject_id=ce.subject_id AND gi.hadm_id=s.hadm_id AND gi.stay_id=s.stay_id
   AND gi.episode_start=s.episode_start AND gi.t0=s.t0 AND gi.charttime=ce.charttime
  WHERE ce.charttime>=s.episode_start AND ce.charttime<s.t0
), lab_map AS MATERIALIZED (
  SELECT * FROM expected_contracts WHERE source_table='mimiciv_hosp.labevents'
), lab_ranked AS MATERIALIZED (
  SELECT m.*,le.subject_id,c.hadm_id,c.episode_start,c.t0,le.labevent_id,le.specimen_id,
         le.charttime,le.storetime,le.valuenum,le.value,coalesce(le.valueuom,'') AS valueuom,
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
         false,labevent_id::text,labevent_id::text,valuenum,value,NULL::numeric,NULL::numeric,NULL::text
  FROM lab_ranked
), input_observed AS MATERIALIZED (
  SELECT m.*,e.subject_id,c.hadm_id,c.episode_start,c.t0,e.stay_id,e.caregiver_id,e.orderid,e.linkorderid,
         e.starttime,e.endtime,e.storetime,e.rate,e.rateuom,e.amount,e.amountuom,e.statusdescription,
         e.originalamount,e.originalrate
  FROM cohort c JOIN mimiciv_icu.inputevents e ON e.hadm_id=c.hadm_id
  JOIN expected_contracts m ON m.source_table='mimiciv_icu.inputevents' AND m.itemid=e.itemid
  WHERE e.starttime<c.t0 AND coalesce(e.endtime,e.starttime)>=c.episode_start
), input_visible AS MATERIALIZED (
  SELECT m.*,e.subject_id,c.hadm_id,c.episode_start,c.t0,e.stay_id,e.caregiver_id,e.orderid,e.linkorderid,
         e.starttime,e.endtime,e.storetime,e.rate,e.rateuom,e.amount,e.amountuom,e.statusdescription,
         e.originalamount,e.originalrate,greatest(e.starttime,e.storetime) AS state_time,
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
         coalesce(CASE WHEN o.contract_role='current4h' THEN r.state_rank=1 AND r.positive_nonstop
                    AND r.state_time>=o.t0-interval '4 hours'
              WHEN o.contract_role='completed_stop' THEN o.endtime IS NOT NULL AND o.storetime IS NOT NULL
                    AND o.endtime<o.t0 AND greatest(o.endtime,o.storetime)<=o.t0
              ELSE o.endtime IS NOT NULL AND o.storetime IS NOT NULL AND o.endtime<o.t0
                    AND greatest(o.endtime,o.storetime)<=o.t0 AND coalesce(o.amount,o.rate,0)>0
                    AND lower(coalesce(o.statusdescription,'')) NOT IN ('paused','rewritten','stopped','discontinued') END,false),
         CASE WHEN o.storetime IS NULL THEN 'MISSING_STORETIME'
              WHEN o.storetime>o.t0 THEN 'AVAILABLE_AFTER_T0'
              WHEN o.contract_role IN ('completed_stop','completed_exposure') AND o.endtime IS NULL THEN 'MISSING_COMPLETION_TIME'
              WHEN o.contract_role IN ('completed_stop','completed_exposure') AND greatest(o.endtime,o.storetime)>o.t0 THEN 'AVAILABLE_AFTER_T0'
              WHEN o.contract_role='completed_exposure'
                AND lower(coalesce(o.statusdescription,'')) IN ('paused','rewritten','stopped','discontinued')
                THEN 'EXPLICIT_NONCOMPLETED_STATE'
              WHEN o.contract_role='completed_exposure' AND coalesce(o.amount,o.rate,0)<=0
                THEN 'NONPOSITIVE_COMPLETED_AMOUNT'
              WHEN o.contract_role='current4h' AND r.state_rank>1 THEN 'SUPERSEDED_VISIBLE_STATE'
              WHEN o.contract_role='current4h' AND r.state_rank=1 AND NOT r.positive_nonstop THEN 'EXPLICIT_NONACTIVE_STATE'
              WHEN o.contract_role='current4h' AND r.state_rank=1 AND r.state_time<o.t0-interval '4 hours' THEN 'STALE_CURRENT_STATE'
              WHEN o.contract_role='current4h' AND o.endtime>o.t0 THEN 'FUTURE_END_PRESENT_NOT_USED'
              ELSE 'OK' END,
         false,NULL::text,
         concat_ws(chr(31),o.subject_id::text,o.hadm_id::text,o.stay_id::text,o.itemid::text,
           to_char(o.starttime,'YYYY-MM-DD"T"HH24:MI:SS.US'),
           coalesce(to_char(o.endtime,'YYYY-MM-DD"T"HH24:MI:SS.US'),chr(92)||'N'),
           coalesce(to_char(o.storetime,'YYYY-MM-DD"T"HH24:MI:SS.US'),chr(92)||'N'),
           coalesce(o.caregiver_id::text,chr(92)||'N'),coalesce(o.orderid::text,chr(92)||'N'),
           coalesce(o.linkorderid::text,chr(92)||'N'),
           coalesce(CASE WHEN o.amount::numeric=0 THEN '0' ELSE
             regexp_replace(regexp_replace((o.amount::numeric)::text,'(\.\d*?)0+$','\1'),'\.$','') END,chr(92)||'N'),
           coalesce(o.amountuom,chr(92)||'N'),
           coalesce(CASE WHEN o.rate::numeric=0 THEN '0' ELSE
             regexp_replace(regexp_replace((o.rate::numeric)::text,'(\.\d*?)0+$','\1'),'\.$','') END,chr(92)||'N'),
           coalesce(o.rateuom,chr(92)||'N'),coalesce(o.statusdescription,chr(92)||'N'),
           coalesce(CASE WHEN o.originalamount::numeric=0 THEN '0' ELSE
             regexp_replace(regexp_replace((o.originalamount::numeric)::text,'(\.\d*?)0+$','\1'),'\.$','') END,chr(92)||'N'),
           coalesce(CASE WHEN o.originalrate::numeric=0 THEN '0' ELSE
             regexp_replace(regexp_replace((o.originalrate::numeric)::text,'(\.\d*?)0+$','\1'),'\.$','') END,chr(92)||'N')),
         CASE WHEN o.contract_role='completed_exposure' THEN o.amount ELSE o.rate END,
         NULL::text,o.rate,o.amount,o.statusdescription
  FROM input_observed o LEFT JOIN input_ranked r
    ON r.subject_id=o.subject_id AND r.hadm_id=o.hadm_id AND r.variable_id=o.variable_id
   AND r.itemid=o.itemid AND r.orderid=o.orderid
), proc_observed AS MATERIALIZED (
  SELECT m.*,p.subject_id,c.hadm_id,c.episode_start,c.t0,p.stay_id,p.caregiver_id,p.orderid,p.linkorderid,
         p.starttime,p.endtime,p.storetime,p.value,p.valueuom,p.statusdescription
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
         coalesce(CASE WHEN o.contract_role='procedure_current' THEN r.state_rank=1
                    AND r.state_time>=o.t0-interval '4 hours'
                    AND lower(coalesce(r.statusdescription,'')) NOT IN ('stopped','finishedrunning','completed','discontinued')
              ELSE o.endtime IS NOT NULL AND o.storetime IS NOT NULL AND o.endtime<o.t0
                    AND greatest(o.endtime,o.storetime)<=o.t0 END,false),
         CASE WHEN o.storetime IS NULL THEN 'MISSING_STORETIME'
              WHEN o.storetime>o.t0 THEN 'AVAILABLE_AFTER_T0'
              WHEN o.contract_role='procedure_current' AND r.state_rank>1 THEN 'SUPERSEDED_VISIBLE_STATE'
              WHEN o.contract_role='procedure_current' AND r.state_rank=1
                AND lower(coalesce(r.statusdescription,'')) IN ('stopped','finishedrunning','completed','discontinued')
                THEN 'EXPLICIT_NONACTIVE_STATE'
              WHEN o.contract_role='procedure_current' AND r.state_rank=1 AND r.state_time<o.t0-interval '4 hours' THEN 'STALE_CURRENT_STATE'
              WHEN o.contract_role='procedure_current' AND o.endtime>o.t0 THEN 'FUTURE_END_PRESENT_NOT_USED'
              WHEN o.contract_role='procedure_completed' AND o.endtime IS NULL THEN 'MISSING_COMPLETION_TIME'
              WHEN o.contract_role='procedure_completed' AND greatest(o.endtime,o.storetime)>o.t0 THEN 'AVAILABLE_AFTER_T0'
              ELSE 'OK' END,false,NULL::text,
         concat_ws(chr(31),o.subject_id::text,o.hadm_id::text,o.stay_id::text,o.itemid::text,
           to_char(o.starttime,'YYYY-MM-DD"T"HH24:MI:SS.US'),
           coalesce(to_char(o.endtime,'YYYY-MM-DD"T"HH24:MI:SS.US'),chr(92)||'N'),
           coalesce(to_char(o.storetime,'YYYY-MM-DD"T"HH24:MI:SS.US'),chr(92)||'N'),
           coalesce(o.caregiver_id::text,chr(92)||'N'),coalesce(o.orderid::text,chr(92)||'N'),
           coalesce(o.linkorderid::text,chr(92)||'N'),
           coalesce(CASE WHEN o.value::numeric=0 THEN '0' ELSE
             regexp_replace(regexp_replace((o.value::numeric)::text,'(\.\d*?)0+$','\1'),'\.$','') END,chr(92)||'N'),
           coalesce(o.valueuom,chr(92)||'N'),coalesce(o.statusdescription,chr(92)||'N')),
         o.value,NULL::text,NULL::numeric,NULL::numeric,o.statusdescription
  FROM proc_observed o LEFT JOIN proc_ranked r
    ON r.subject_id=o.subject_id AND r.hadm_id=o.hadm_id AND r.variable_id=o.variable_id
   AND r.itemid=o.itemid AND r.orderid=o.orderid
), vent_contracts AS MATERIALIZED (
  SELECT * FROM expected_contracts
  WHERE variable_id IN ('invasive_vent_active_t0','hours_since_invasive_vent_end')
), vent_observed AS MATERIALIZED (
  SELECT m.*,ce.subject_id,s.hadm_id,s.stay_id,s.episode_start,s.t0,ce.charttime,ce.storetime,
         ce.caregiver_id,ce.value,ce.valuenum,ce.warning,coalesce(ce.valueuom,'') AS valueuom
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
         coalesce((o.storetime IS NOT NULL AND greatest(o.charttime,o.storetime)<=o.t0
          AND es.evidence_state IN ('active','nonactive')
          AND CASE WHEN o.variable_id='invasive_vent_active_t0' THEN coalesce(ps.active_t0,false)
                   ELSE NOT coalesce(ps.active_t0,false) AND ps.last_interval_end IS NOT NULL END),false),
         CASE WHEN o.storetime IS NULL THEN 'MISSING_STORETIME'
              WHEN greatest(o.charttime,o.storetime)>o.t0 THEN 'AVAILABLE_AFTER_T0'
              WHEN es.evidence_state IS NULL OR es.evidence_state='other' THEN 'UNCLASSIFIED_VENT_EVIDENCE'
              WHEN o.variable_id='invasive_vent_active_t0' AND NOT coalesce(ps.active_t0,false) THEN 'NO_ACTIVE_INTERVAL_AT_T0'
              WHEN o.variable_id='hours_since_invasive_vent_end' AND ps.last_interval_end IS NULL THEN 'NO_CONFIRMED_INTERVAL_END'
              ELSE 'OK' END,false,NULL::text,
         concat_ws(chr(31),o.subject_id::text,o.hadm_id::text,o.stay_id::text,o.itemid::text,
           to_char(o.charttime,'YYYY-MM-DD"T"HH24:MI:SS.US'),
           coalesce(to_char(o.storetime,'YYYY-MM-DD"T"HH24:MI:SS.US'),chr(92)||'N'),
           coalesce(o.caregiver_id::text,chr(92)||'N'),coalesce(o.value,chr(92)||'N'),
           coalesce(CASE WHEN o.valuenum::numeric=0 THEN '0' ELSE
             regexp_replace(regexp_replace((o.valuenum::numeric)::text,'(\.\d*?)0+$','\1'),'\.$','') END,chr(92)||'N'),
           coalesce(o.valueuom,chr(92)||'N'),coalesce(o.warning::text,chr(92)||'N')),
         o.valuenum,o.value,NULL::numeric,NULL::numeric,NULL::text
  FROM vent_observed o
  LEFT JOIN vent_evidence_state es USING(subject_id,hadm_id,stay_id,episode_start,t0,charttime)
  LEFT JOIN vent_patient_state ps USING(subject_id,hadm_id,t0)
), raw_precheck(variable_id,source_contract_id,source_table,itemid,source_unit,subject_id,hadm_id,episode_start,t0,
       event_time,storetime,availability_time,endtime,time_included,anomaly_code,future_endtime_used,
       native_labevent_id,key_payload,raw_numeric_value,raw_text_value,rate,amount,status_text) AS MATERIALIZED (
  SELECT * FROM ce_source UNION ALL SELECT * FROM lab_source UNION ALL SELECT * FROM input_source
  UNION ALL SELECT * FROM proc_source UNION ALL SELECT * FROM vent_source
), value_rules AS MATERIALIZED (
  SELECT r.*,m.unit_rule,m.min_value,m.max_value,
         coalesce(CASE
           WHEN m.unit_rule IN ('categorical','pupil_category') THEN nullif(trim(r.raw_text_value),'') IS NOT NULL
           WHEN m.unit_rule IN ('itemid_score','itemid223835_percent') THEN r.raw_numeric_value IS NOT NULL
           WHEN m.unit_rule='temperature_c' THEN r.source_unit IN ('°C','Deg. C','C')
           WHEN m.unit_rule='temperature_f' THEN r.source_unit IN ('°F','Deg. F','F')
           WHEN m.unit_rule='percent' THEN r.source_unit IN ('%','')
           WHEN m.unit_rule='binary_activity' THEN
             (coalesce(r.raw_numeric_value>0,false) OR nullif(trim(r.raw_text_value),'') IS NOT NULL)
           WHEN m.unit_rule='positive_rate' AND m.contract_role='completed_stop' THEN true
           WHEN m.unit_rule='positive_rate' THEN r.rate>0
           WHEN m.unit_rule='completed_positive_amount' THEN r.amount>0
           WHEN m.unit_rule='procedure_state' THEN true
           WHEN m.unit_rule='mEq/L_or_mmol/L' THEN r.source_unit IN ('mEq/L','mmol/L')
           WHEN m.unit_rule='ratio' THEN r.source_unit IN ('','ratio')
           ELSE r.source_unit=m.unit_rule END,false) AS frozen_unit_valid,
         coalesce(CASE
           WHEN m.min_value IS NULL AND m.max_value IS NULL THEN true
           WHEN m.unit_rule='binary_activity' AND r.raw_numeric_value IS NULL
             AND nullif(trim(r.raw_text_value),'') IS NOT NULL THEN true
           WHEN r.raw_numeric_value IS NULL THEN false
           WHEN m.unit_rule='temperature_f' THEN
             (m.min_value IS NULL OR r.raw_numeric_value>=(m.min_value-32.0)*5.0/9.0)
             AND (m.max_value IS NULL OR r.raw_numeric_value<=(m.max_value-32.0)*5.0/9.0)
           WHEN m.unit_rule='itemid223835_percent' THEN
             (m.min_value IS NULL OR r.raw_numeric_value>=m.min_value/100.0)
             AND (m.max_value IS NULL OR r.raw_numeric_value<=m.max_value/100.0)
           ELSE (m.min_value IS NULL OR r.raw_numeric_value>=m.min_value)
             AND (m.max_value IS NULL OR r.raw_numeric_value<=m.max_value) END,false) AS frozen_range_valid,
         coalesce(r.event_time>=r.episode_start AND r.event_time<r.t0
           AND r.availability_time>=r.event_time AND r.availability_time<=r.t0,false) AS value_time_valid
  FROM raw_precheck r JOIN expected_contracts m USING(source_contract_id)
), raw AS MATERIALIZED (
  SELECT variable_id,source_contract_id,source_table,itemid,source_unit,subject_id,hadm_id,episode_start,t0,
         event_time,storetime,availability_time,endtime,coalesce(time_included,false) AS time_included,
         (coalesce(time_included,false) AND anomaly_code IN ('OK','FUTURE_END_PRESENT_NOT_USED') AND frozen_unit_valid
           AND frozen_range_valid AND value_time_valid AND NOT future_endtime_used) AS included,
         CASE
           WHEN NOT coalesce(time_included,false) THEN
             CASE WHEN anomaly_code IS NULL OR anomaly_code='OK' THEN 'TIME_OR_STATE_NOT_ELIGIBLE' ELSE anomaly_code END
           WHEN anomaly_code IS NULL OR anomaly_code NOT IN ('OK','FUTURE_END_PRESENT_NOT_USED')
             THEN coalesce(anomaly_code,'SOURCE_ANOMALY_MISSING')
           WHEN NOT frozen_unit_valid THEN 'VALUE_UNKNOWN_OR_INVALID_UNIT'
           WHEN NOT frozen_range_valid THEN 'VALUE_OUTSIDE_FROZEN_RANGE'
           WHEN event_time IS NULL OR availability_time IS NULL THEN 'VALUE_TIME_MISSING'
           WHEN availability_time<event_time THEN 'AVAILABILITY_BEFORE_EVENT'
           WHEN NOT value_time_valid THEN 'VALUE_TIME_OUTSIDE_FROZEN_WINDOW'
           WHEN future_endtime_used THEN 'FUTURE_ENDTIME_USED'
           ELSE 'OK' END AS exclusion_code,
         CASE WHEN anomaly_code='FUTURE_END_PRESENT_NOT_USED'
           AND coalesce(time_included,false) AND frozen_unit_valid AND frozen_range_valid
           AND value_time_valid AND NOT future_endtime_used
           THEN 'FUTURE_END_PRESENT_NOT_USED' ELSE '' END AS warning_code,
         future_endtime_used,native_labevent_id,key_payload,raw_numeric_value,raw_text_value,rate,amount,status_text
  FROM value_rules
), keyed AS MATERIALIZED ( -- CANONICAL_RECORD_KEY_V1
  SELECT *,row_number() OVER (PARTITION BY source_contract_id,key_payload ORDER BY key_payload) AS duplicate_ordinal
  FROM raw
)
SELECT CASE WHEN source_table='mimiciv_hosp.labevents' THEN native_labevent_id
            ELSE upper(encode(sha256(convert_to(source_contract_id||chr(31)||key_payload||chr(31)||duplicate_ordinal::text,'UTF8')),'hex'))
       END AS record_key,
       variable_id,source_contract_id,split_part(source_table,'.',1) AS source_schema,source_table,itemid,
       subject_id,hadm_id,episode_start,t0,event_time,storetime,availability_time,endtime,source_unit,
       raw_numeric_value,raw_text_value,rate,amount,status_text,
       warning_code,time_included,included,exclusion_code
FROM keyed
ORDER BY source_contract_id,record_key;
ROLLBACK;
