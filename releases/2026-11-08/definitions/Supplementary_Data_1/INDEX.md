# Supplementary Data 1 — definitions and frozen SQL

本附件仅包含非患者级操作定义、输入依赖、预处理规格和未注入病例值的冻结SQL模板。文件逐一来自当前Project_B定义源或既有冻结SQL，未整目录复制历史附件。逐文件来源、SHA256及逐字节一致性见MANIFEST.json。

## 队列更正边界

COHORT_OUTCOME.sql是未改写的冻结基础筛选SQL，单独运行该SQL不等于重建最终更正的3,244人队列。开发后另有1例研究者裁定不属于目标人群的脑死亡/器官获取病例，在受限研究流水线中排除；公开附件不提供该病例编号、患者清单或含病例值的更正文件。这不是新增24小时时差排除规则。基础SQL与最终更正人群的区别必须保留，不能把未改写SQL标成已经包含该人工裁定。

## 文件用途

- CORE_VARIABLE_DICTIONARY.csv和CORE_MATRIX_SPEC.csv：冻结临床核心定义与设计列。
- EXTENDED_VARIABLE_REGISTRY_A1_FROZEN.csv、EXTENDED_REPRESENTATION_SPEC.csv、CLINICAL_DOMAIN_MAP.csv：扩展信号、表示与临床分组。
- SOURCE_TIME_CONTRACTS.csv、INITIAL_NEURO_ASSESSABILITY_RULES.csv、A3_VALUE_EXTRACTION_SPEC.csv及A4_FIELD_DICTIONARY.csv：来源时间、初始神经评估与值提取规则；初始完整GCS须在ICU episode开始后前6小时，不可用更晚记录替代。
- PREPROCESSING_SPEC.json和FOLD_LOCAL_PREPROCESSING_RULES.json：训练分区内预处理规则，不含拟合后的患者统计或模型权重。
- COMPACT_MANUAL_INPUT_SPEC.json：通用手工输入预算定义；MANUAL_INPUT_DICTIONARY.json为本次更正后锁定10项模型的纯元数据，明确包含血红蛋白。不得据历史INR版字典替换它。
- COHORT_OUTCOME.sql、CORE_BASELINE_COURSE.sql、CORE_VENTILATION.sql、CORE_NEURO_WPR.sql、MODIFIED_NON_NEURO_SOFA.sql、EXTENSION_AUDIT_TEMPLATE.sql及EXTENSION_EXTRACTION_TEMPLATE.sql：七份原始冻结SQL，保留原模板占位符。若当前a3_development_value_extract.sql与历史提取模板不同，则另名附上，不能覆盖历史模板。

SQL内subject_id/hadm_id/stay_id为模式列名，itemid/ICD为临床映射码；它们不是实际患者编号。__COHORT_VALUES__等保持为未填入患者数据的占位符。运行仍须授权原始数据库、受限更正流水线与既有环境；不要为了阅读附件重新提取时间队列或重做拟合/bootstrap。
