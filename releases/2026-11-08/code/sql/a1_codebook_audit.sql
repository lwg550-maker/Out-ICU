SET default_transaction_read_only=on;
BEGIN READ ONLY;
SHOW transaction_read_only;
SET LOCAL statement_timeout='10min';
WITH icu_map(variable_id,itemid,itemid_priority,mapping_role) AS (
  VALUES
  ('initial_gcs_total',220739,1,'eye'),('initial_gcs_total',223900,2,'verbal'),('initial_gcs_total',223901,3,'motor'),
  ('initial_pupil_nonreactive_count',227121,1,'right'),('initial_pupil_nonreactive_count',227288,2,'left'),
  ('icp_value',220765,1,'numeric'),
  ('invasive_vent_active_t0',223849,1,'mode'),('invasive_vent_active_t0',229314,2,'mode'),
  ('invasive_vent_active_t0',224688,3,'pressure'),('invasive_vent_active_t0',224684,4,'pressure'),
  ('invasive_vent_active_t0',224685,5,'pressure'),('invasive_vent_active_t0',224686,6,'pressure'),
  ('invasive_vent_active_t0',224696,7,'pressure'),('invasive_vent_active_t0',220339,8,'peep'),
  ('invasive_vent_active_t0',224700,9,'peep'),('invasive_vent_active_t0',226732,10,'airway'),
  ('hours_since_invasive_vent_end',223849,1,'mode'),('hours_since_invasive_vent_end',229314,2,'mode'),
  ('hours_since_invasive_vent_end',224688,3,'pressure'),('hours_since_invasive_vent_end',224684,4,'pressure'),
  ('hours_since_invasive_vent_end',224685,5,'pressure'),('hours_since_invasive_vent_end',224686,6,'pressure'),
  ('hours_since_invasive_vent_end',224696,7,'pressure'),('hours_since_invasive_vent_end',220339,8,'peep'),
  ('hours_since_invasive_vent_end',224700,9,'peep'),('hours_since_invasive_vent_end',226732,10,'airway'),
  ('vasopressor_active_t0',221289,1,'drug'),('vasopressor_active_t0',229617,2,'drug'),
  ('vasopressor_active_t0',221662,3,'drug'),('vasopressor_active_t0',221653,4,'drug'),
  ('vasopressor_active_t0',221906,5,'drug'),('vasopressor_active_t0',221749,6,'drug'),
  ('vasopressor_active_t0',229630,7,'drug'),('vasopressor_active_t0',229631,8,'drug'),
  ('vasopressor_active_t0',229632,9,'drug'),('vasopressor_active_t0',222315,10,'drug'),
  ('vasopressor_active_t0',229709,11,'drug'),('vasopressor_active_t0',229764,12,'drug'),
  ('hours_since_vasopressor_end',221289,1,'drug'),('hours_since_vasopressor_end',229617,2,'drug'),
  ('hours_since_vasopressor_end',221662,3,'drug'),('hours_since_vasopressor_end',221653,4,'drug'),
  ('hours_since_vasopressor_end',221906,5,'drug'),('hours_since_vasopressor_end',221749,6,'drug'),
  ('hours_since_vasopressor_end',229630,7,'drug'),('hours_since_vasopressor_end',229631,8,'drug'),
  ('hours_since_vasopressor_end',229632,9,'drug'),('hours_since_vasopressor_end',222315,10,'drug'),
  ('hours_since_vasopressor_end',229709,11,'drug'),('hours_since_vasopressor_end',229764,12,'drug'),
  ('sedative_active_t0',221668,1,'drug'),('sedative_active_t0',222168,2,'drug'),
  ('sedative_active_t0',225150,3,'drug'),('sedative_active_t0',229420,4,'drug'),
  ('nmb_active_t0',221555,1,'drug'),('nmb_active_t0',222062,2,'drug'),('nmb_active_t0',229233,3,'drug'),
  ('heart_rate',220045,1,'numeric'),
  ('systolic_bp',220179,1,'numeric'),('systolic_bp',220050,2,'numeric'),
  ('mean_arterial_pressure',220181,1,'numeric'),('mean_arterial_pressure',220052,2,'numeric'),('mean_arterial_pressure',225312,3,'numeric'),
  ('respiratory_rate',220210,1,'numeric'),('respiratory_rate',224690,2,'numeric'),
  ('temperature_c',223762,1,'celsius'),('temperature_c',223761,2,'fahrenheit'),
  ('spo2',220277,1,'numeric'),('fio2_fraction',223835,1,'numeric'),
  ('peep_cmh2o',220339,1,'numeric'),('peep_cmh2o',224700,2,'numeric')
), lab_map(variable_id,itemid,itemid_priority,mapping_role) AS (
  VALUES
  ('hemoglobin',51222,1,'numeric'),('wbc_absolute',51301,1,'numeric'),
  ('platelet_absolute',51265,1,'numeric'),('sodium',50983,1,'numeric'),
  ('potassium',50971,1,'numeric'),('bicarbonate',50882,1,'numeric'),
  ('bun',51006,1,'numeric'),('creatinine',50912,1,'numeric'),
  ('glucose',50931,1,'numeric'),('lactate',50813,1,'numeric'),
  ('inr',51237,1,'numeric'),('albumin',50862,1,'numeric'),('bilirubin_total',50885,1,'numeric')
), fixed_icu AS (
  SELECT m.variable_id,'mimiciv_icu'::text AS source_schema,d.linksto AS source_table,
         d.itemid,d.label,d.unitname AS codebook_unit,d.category,d.linksto,
         m.itemid_priority,m.mapping_role,'FIXED_CANDIDATE'::text AS mapping_source
  FROM icu_map m LEFT JOIN mimiciv_icu.d_items d USING(itemid)
), fixed_lab AS (
  SELECT m.variable_id,'mimiciv_hosp'::text,'labevents'::text,d.itemid,d.label,
         NULL::text AS codebook_unit,d.category,'labevents'::text AS linksto,
         m.itemid_priority,m.mapping_role,'FIXED_CANDIDATE'::text
  FROM lab_map m LEFT JOIN mimiciv_hosp.d_labitems d USING(itemid)
), discovery AS (
  SELECT CASE WHEN lower(d.label) ~ '(mannitol|hypertonic.*saline|sodium chloride.*(3%|23\.4%))'
              THEN 'hyperosmolar_ever' ELSE 'rrt_ever/rrt_active_t0' END AS variable_id,
         'mimiciv_icu'::text,d.linksto,d.itemid,d.label,d.unitname,d.category,d.linksto,
         999 AS itemid_priority,'discovery'::text AS mapping_role,'LABEL_QUERY'::text AS mapping_source
  FROM mimiciv_icu.d_items d
  WHERE (d.linksto='inputevents' AND lower(d.label) ~ '(mannitol|hypertonic.*saline|sodium chloride.*(3%|23\.4%))')
     OR (d.linksto IN ('chartevents','procedureevents') AND
         (lower(d.label) ~ '(dialysis|cvvh|hemofiltration|renal replacement|\mrrt\M)'
          OR lower(coalesce(d.category,'')) ~ '(dialysis|cvvh|renal replacement)'))
)
SELECT variable_id,source_schema,source_table,itemid,label,codebook_unit,category,linksto,
       itemid_priority,mapping_role,mapping_source
FROM fixed_icu
UNION ALL
SELECT * FROM fixed_lab
UNION ALL
SELECT * FROM discovery
ORDER BY variable_id,itemid_priority,itemid;
ROLLBACK;
