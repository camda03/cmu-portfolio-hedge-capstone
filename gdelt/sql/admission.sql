-- Apply the same Expanded theme rules to canonical GKG records used for the live query cohort.

CREATE OR REPLACE FUNCTION `${dataset}.gkg_expanded_rule_ids_v01`(labels ARRAY<STRING>)
RETURNS ARRAY<STRING>
AS ((WITH normalized AS (SELECT ARRAY(SELECT DISTINCT normalized_label FROM (SELECT TRIM(REGEXP_REPLACE(NORMALIZE_AND_CASEFOLD(label,NFKC),r'[\s\p{Z}\x{0085}\x{001C}-\x{001F}]+',' ')) AS normalized_label FROM UNNEST(labels) AS label) WHERE normalized_label!='' ORDER BY normalized_label) AS themes)
 SELECT ARRAY_CONCAT(IF((EXISTS (SELECT 1 FROM UNNEST(themes) AS feature WHERE feature IN ("econ_centralbank","econ_inflation","econ_interest_rates","epu_cats_monetary_policy"))),["D01"],ARRAY<STRING>[]),
 IF((EXISTS (SELECT 1 FROM UNNEST(themes) AS feature WHERE feature IN ("econ_cost_of_living","econ_housing_prices","unemployment","wb_471_economic_growth"))),["D02"],ARRAY<STRING>[]),
 IF((EXISTS (SELECT 1 FROM UNNEST(themes) AS feature WHERE feature IN ("econ_budget_deficit","econ_taxation","epu_cats_fiscal_policy","wb_445_fiscal_policy"))),["D03"],ARRAY<STRING>[]),
 IF((EXISTS (SELECT 1 FROM UNNEST(themes) AS feature WHERE feature IN ("econ_currency_exchange_rate","econ_freetrade","econ_trade_dispute","wb_2575_trade_policy_and_investment_agreements","wb_775_trade_policy_and_integration"))),["D04"],ARRAY<STRING>[]),
 IF((EXISTS (SELECT 1 FROM UNNEST(themes) AS feature WHERE feature IN ("econ_earningsreport","econ_stockmarket","epu_cats_financial_regulation","wb_332_capital_markets"))),["D05"],ARRAY<STRING>[]),
 IF((EXISTS (SELECT 1 FROM UNNEST(themes) AS feature WHERE feature IN ("epu_cats_sovereign_debt_currency_crises","wb_1098_monetary_and_financial_stability","wb_1245_financial_vulnerability_and_risks"))),["D06"],ARRAY<STRING>[]),
 IF((EXISTS (SELECT 1 FROM UNNEST(themes) AS feature WHERE feature IN ("econ_oilprice","wb_539_oil_and_gas_policy_strategy_and_institutions"))),["D07"],ARRAY<STRING>[]),
 IF((EXISTS (SELECT 1 FROM UNNEST(themes) AS feature WHERE feature IN ("blockade","ceasefire","sanctions"))),["D08"],ARRAY<STRING>[]),
 IF((EXISTS (SELECT 1 FROM UNNEST(themes) AS feature WHERE feature IN ("health_pandemic","wb_2167_pandemics"))),["D09"],ARRAY<STRING>[]),
 IF((EXISTS (SELECT 1 FROM UNNEST(themes) AS feature WHERE feature IN ("econ_bankruptcy"))) AND (EXISTS (SELECT 1 FROM UNNEST(themes) AS feature WHERE feature IN ("wb_1234_banking_institutions","wb_317_bank_regulation_and_supervision","wb_318_financial_architecture_and_banking"))),["C01"],ARRAY<STRING>[]),
 IF((EXISTS (SELECT 1 FROM UNNEST(themes) AS feature WHERE feature IN ("armedconflict","maritime_incident","natural_disaster","negotiations","political_turmoil","wb_2480_post_conflict_reconstruction","wb_2482_reconciliation"))) AND (EXISTS (SELECT 1 FROM UNNEST(themes) AS feature WHERE feature IN ("env_naturalgas","env_oil","wb_167_ports","wb_2299_pipelines","wb_793_transport_and_logistics_services"))),["C02"],ARRAY<STRING>[]),
 IF((EXISTS (SELECT 1 FROM UNNEST(themes) AS feature WHERE feature IN ("strike"))) AND (EXISTS (SELECT 1 FROM UNNEST(themes) AS feature WHERE feature IN ("wb_1281_manufacturing","wb_167_ports","wb_2299_pipelines","wb_508_power_systems","wb_793_transport_and_logistics_services"))),["C03"],ARRAY<STRING>[]),
 IF((EXISTS (SELECT 1 FROM UNNEST(themes) AS feature WHERE feature IN ("crisislex_t04_infrastructure","cyber_attack","natural_disaster","power_outage"))) AND (EXISTS (SELECT 1 FROM UNNEST(themes) AS feature WHERE feature IN ("env_naturalgas","env_oil","wb_1234_banking_institutions","wb_1281_manufacturing","wb_167_ports","wb_2299_pipelines","wb_317_bank_regulation_and_supervision","wb_318_financial_architecture_and_banking","wb_508_power_systems","wb_793_transport_and_logistics_services"))),["C04"],ARRAY<STRING>[]),
 IF((EXISTS (SELECT 1 FROM UNNEST(themes) AS feature WHERE feature IN ("election","epu_uncertainty","political_turmoil"))) AND (EXISTS (SELECT 1 FROM UNNEST(themes) AS feature WHERE feature IN ("econ_debt","epu_economy","epu_economy_historic","epu_policy_budget","epu_policy_spending","wb_1281_manufacturing","wb_2748_employment","wb_698_trade","wb_856_wages"))),["C05"],ARRAY<STRING>[]),
 IF((EXISTS (SELECT 1 FROM UNNEST(themes) AS feature WHERE feature IN ("epu_policy","epu_policy_government","epu_policy_law","general_government"))) AND (EXISTS (SELECT 1 FROM UNNEST(themes) AS feature WHERE feature IN ("env_naturalgas","env_oil","wb_1234_banking_institutions","wb_1281_manufacturing","wb_167_ports","wb_2299_pipelines","wb_317_bank_regulation_and_supervision","wb_318_financial_architecture_and_banking","wb_508_power_systems","wb_793_transport_and_logistics_services"))),["C06"],ARRAY<STRING>[])) FROM normalized));

CREATE TABLE `${dataset}.filtered_live_${batch_id}_parsed_v01` CLUSTER BY raw_revision_sha256 AS
SELECT r.raw_revision_sha256 AS raw_revision_sha256,r.batch_month AS batch_month,r.candidate_day AS candidate_day,r.candidate_order AS candidate_order,r.source_rows_for_raw_url_day AS source_rows_for_raw_url_day,r.source_record_ids_for_raw_url_day AS source_record_ids_for_raw_url_day,`${dataset}.gkg_parse_source_v01`(TO_JSON_STRING(STRUCT(r.`GKGRECORDID` AS `GKGRECORDID`,r.`DATE` AS `DATE`,r.`SourceCollectionIdentifier` AS `SourceCollectionIdentifier`,r.`SourceCommonName` AS `SourceCommonName`,r.`DocumentIdentifier` AS `DocumentIdentifier`,r.`Themes` AS `Themes`,r.`V2Themes` AS `V2Themes`,r.`Persons` AS `Persons`,r.`V2Persons` AS `V2Persons`,r.`Organizations` AS `Organizations`,r.`V2Organizations` AS `V2Organizations`,r.`Locations` AS `Locations`,r.`V2Locations` AS `V2Locations`,r.`TranslationInfo` AS `TranslationInfo`,r.`Title` AS `Title`,r.`ExtrasXML` AS `ExtrasXML`))) AS p
FROM `${dataset}.filtered_live_${batch_id}_raw_v01` AS r ;

CREATE TABLE `${dataset}.filtered_live_${batch_id}_normalized_v01` CLUSTER BY document_id AS
WITH parsed AS (
 SELECT r.raw_revision_sha256 AS raw_revision_sha256,r.batch_month AS batch_month,r.candidate_day AS candidate_day,r.candidate_order AS candidate_order,r.source_rows_for_raw_url_day AS source_rows_for_raw_url_day,r.source_record_ids_for_raw_url_day AS source_record_ids_for_raw_url_day,r.p AS p FROM `${dataset}.filtered_live_${batch_id}_parsed_v01` AS r 
), normalized AS (
 SELECT raw_revision_sha256,batch_month,candidate_day,candidate_order,source_rows_for_raw_url_day,source_record_ids_for_raw_url_day,p,TRIM(REGEXP_REPLACE(NORMALIZE(p.title,NFKC),r'[\s\p{Z}\x{0085}\x{001C}-\x{001F}]+',' ')) AS title,TRIM(REGEXP_REPLACE(NORMALIZE_AND_CASEFOLD(p.title,NFKC),r'[\s\p{Z}\x{0085}\x{001C}-\x{001F}]+',' ')) AS metadata_title,
 ARRAY(SELECT DISTINCT normalized_label FROM (SELECT TRIM(REGEXP_REPLACE(NORMALIZE_AND_CASEFOLD(label,NFKC),r'[\s\p{Z}\x{0085}\x{001C}-\x{001F}]+',' ')) AS normalized_label FROM UNNEST(p.themes) AS label) WHERE normalized_label!='' ORDER BY normalized_label) AS themes,
 ARRAY(SELECT DISTINCT normalized_label FROM (SELECT TRIM(REGEXP_REPLACE(NORMALIZE_AND_CASEFOLD(label,NFKC),r'[\s\p{Z}\x{0085}\x{001C}-\x{001F}]+',' ')) AS normalized_label FROM UNNEST(p.persons) AS label) WHERE normalized_label!='' ORDER BY normalized_label) AS persons,
 ARRAY(SELECT DISTINCT normalized_label FROM (SELECT TRIM(REGEXP_REPLACE(NORMALIZE_AND_CASEFOLD(label,NFKC),r'[\s\p{Z}\x{0085}\x{001C}-\x{001F}]+',' ')) AS normalized_label FROM UNNEST(p.organizations) AS label) WHERE normalized_label!='' ORDER BY normalized_label) AS organizations,
 ARRAY(SELECT DISTINCT normalized_label FROM (SELECT TRIM(REGEXP_REPLACE(NORMALIZE_AND_CASEFOLD(label,NFKC),r'[\s\p{Z}\x{0085}\x{001C}-\x{001F}]+',' ')) AS normalized_label FROM UNNEST(p.locations) AS label) WHERE normalized_label!='' ORDER BY normalized_label) AS locations,
 IF(REGEXP_CONTAINS(p.gkg_record_id,r'^[0-9]{14}-T?[0-9]+$'),SAFE.PARSE_TIMESTAMP('%Y%m%d%H%M%S',SUBSTR(p.gkg_record_id,1,14),'UTC'),NULL) AS observed_at,
 IF(REGEXP_CONTAINS(p.gkg_date,r'^[0-9]{14}$'),SAFE.PARSE_TIMESTAMP('%Y%m%d%H%M%S',p.gkg_date,'UTC'),NULL) AS source_date,
 IF(p.document_identity_json IS NOT NULL,LOWER(TO_HEX(SHA256(CONCAT('gkg-document-v1\n',p.document_identity_json)))),NULL) AS document_id,
 IF(p.source_payload_canonical_json IS NOT NULL,LOWER(TO_HEX(SHA256(CONCAT('gkg-named-payload-v1\n',p.source_payload_canonical_json)))),NULL) AS source_payload_sha256
 FROM parsed
), with_full_text AS (
 SELECT raw_revision_sha256,batch_month,candidate_day,candidate_order,source_rows_for_raw_url_day,source_record_ids_for_raw_url_day,p,title,metadata_title,themes,persons,organizations,locations,observed_at,source_date,document_id,source_payload_sha256,
 ARRAY_TO_STRING(ARRAY_CONCAT(IF(metadata_title!='',[CONCAT('title: ',metadata_title)],ARRAY<STRING>[]),IF(ARRAY_LENGTH(themes)>0,[CONCAT('themes: ',ARRAY_TO_STRING(themes,'; '))],ARRAY<STRING>[]),IF(ARRAY_LENGTH(persons)>0,[CONCAT('persons: ',ARRAY_TO_STRING(persons,'; '))],ARRAY<STRING>[]),IF(ARRAY_LENGTH(organizations)>0,[CONCAT('organizations: ',ARRAY_TO_STRING(organizations,'; '))],ARRAY<STRING>[]),IF(ARRAY_LENGTH(locations)>0,[CONCAT('locations: ',ARRAY_TO_STRING(locations,'; '))],ARRAY<STRING>[])),'\n') AS full_metadata_text,
 IF(document_id IS NOT NULL,TO_JSON_STRING(STRUCT(document_id AS document_key,p.gkg_record_id AS gkg_record_id)),NULL) AS source_record_identity_json
 FROM normalized
), bounded AS (
 SELECT raw_revision_sha256,batch_month,candidate_day,candidate_order,source_rows_for_raw_url_day,source_record_ids_for_raw_url_day,p,title,observed_at,source_date,document_id,source_payload_sha256,source_record_identity_json,
 IF(source_record_identity_json IS NOT NULL,LOWER(TO_HEX(SHA256(CONCAT('gkg-source-record-v02-document-scoped\n',source_record_identity_json)))),NULL) AS source_record_key,
 IF(BYTE_LENGTH(full_metadata_text)<=6000,
 STRUCT(full_metadata_text AS metadata_text,metadata_title AS title,themes AS themes,persons AS persons,organizations AS organizations,locations AS locations,TO_JSON_STRING(STRUCT(6000 AS maximum_utf8_bytes,BYTE_LENGTH(full_metadata_text) AS original_utf8_bytes,
 BYTE_LENGTH(full_metadata_text) AS retained_utf8_bytes,ARRAY<STRING>[] AS truncated_fields,
 STRUCT(0 AS title,0 AS themes,0 AS persons,0 AS organizations,0 AS locations) AS omitted_units,
 'full_if_total_fits_else_balanced_complete_label_prefixes_with_redistribution' AS policy)) AS truncation_json),
 `${dataset}.gkg_bound_metadata_v03`(metadata_title,themes,persons,organizations,locations)) AS b
 FROM with_full_text
), tokenized AS (
 SELECT raw_revision_sha256,batch_month,candidate_day,candidate_order,source_rows_for_raw_url_day,source_record_ids_for_raw_url_day,p,title,observed_at,source_date,document_id,source_payload_sha256,source_record_identity_json,source_record_key,b,
 IF(source_record_key IS NOT NULL AND source_payload_sha256 IS NOT NULL,TO_JSON_STRING([source_record_key,source_payload_sha256]),NULL) AS source_revision_identity_json,
 ARRAY_CONCAT(ARRAY(SELECT CONCAT('theme:',term) FROM UNNEST(b.themes) AS term),
 ARRAY(SELECT CONCAT('word:',term) FROM UNNEST(REGEXP_EXTRACT_ALL(ARRAY_TO_STRING([b.title,ARRAY_TO_STRING(b.persons,' '),ARRAY_TO_STRING(b.organizations,' '),ARRAY_TO_STRING(b.locations,' ')],' '),r'[\p{L}\p{N}]+')) AS term)) AS lex_tokens
 FROM bounded
), assessed AS (
 SELECT raw_revision_sha256,batch_month,candidate_day,candidate_order,source_rows_for_raw_url_day,source_record_ids_for_raw_url_day,p,title,observed_at,document_id,source_payload_sha256,source_record_identity_json,source_record_key,b,source_revision_identity_json,lex_tokens,
 ARRAY_CONCAT(p.parse_reasons,
 IF(p.source_collection IS NULL OR p.source_collection!='1',['not_web_source_collection'],ARRAY<STRING>[]),
 IF(observed_at IS NULL,['invalid_record_timestamp'],ARRAY<STRING>[]),
 IF(p.gkg_date='0',['unknown_source_date'],IF(source_date IS NULL,['invalid_source_date'],IF(source_date!=observed_at,['source_date_record_timestamp_conflict'],ARRAY<STRING>[]))),
 IF(observed_at IS NULL OR candidate_day IS NULL OR candidate_day!=FORMAT_DATE('%Y-%m-%d',DATE(observed_at)),['candidate_day_observation_date_conflict'],ARRAY<STRING>[]),
 IF(b.metadata_text='',['empty_metadata'],ARRAY<STRING>[]),
 IF(ARRAY_LENGTH(lex_tokens)=0,['zero_lexical_tokens'],ARRAY<STRING>[])) AS rejection_reasons
 FROM tokenized
)
SELECT raw_revision_sha256,batch_month,candidate_day,candidate_order,source_rows_for_raw_url_day,source_record_ids_for_raw_url_day,document_id,source_record_key,
 IF(source_revision_identity_json IS NOT NULL,LOWER(TO_HEX(SHA256(CONCAT('gkg-observation-revision-v1\n',source_revision_identity_json)))),NULL) AS source_revision_key,
 source_payload_sha256,p.gkg_record_id AS gkg_record_id,p.source_collection AS source_collection,p.source_name AS source_name,
 p.document_url AS document_url,DATE(observed_at) AS observed_day,observed_at,title,
 b.metadata_text AS metadata_text,LOWER(TO_HEX(SHA256(b.metadata_text))) AS metadata_sha256,
 lex_tokens,ARRAY_LENGTH(lex_tokens) AS lexical_token_count,
 (SELECT COUNT(DISTINCT term) FROM UNNEST(lex_tokens) AS term) AS lexical_unique_token_count,
 ARRAY_CONCAT(IF(title='',['title_missing'],ARRAY<STRING>[]),
 IF(title='' AND (IF(ARRAY_LENGTH(b.themes)>0,1,0)+IF(ARRAY_LENGTH(b.persons)>0,1,0)+IF(ARRAY_LENGTH(b.organizations)>0,1,0)+IF(ARRAY_LENGTH(b.locations)>0,1,0))<=1,['single_feature_family_without_title'],ARRAY<STRING>[]),
 IF((SELECT COUNT(DISTINCT term) FROM UNNEST(lex_tokens) AS term)<10,['fewer_than_10_distinct_lexical_terms'],ARRAY<STRING>[]),
 IF(ARRAY_LENGTH(JSON_VALUE_ARRAY(b.truncation_json,'$.truncated_fields'))>0,['metadata_truncated'],ARRAY<STRING>[]),rejection_reasons) AS quality_flags,
 ARRAY_LENGTH(rejection_reasons)=0 AS admissible,rejection_reasons,b.truncation_json AS truncation_json,
 p.document_identity_json AS source_document_identity_json,source_record_identity_json,source_revision_identity_json,
 'gkg-bq-identity-v01-conservative-ascii-host' AS normalization_version,'gkg-bq-metadata-v03-total-first-complete-labels-6000utf8' AS representation_version,'gkg-bq-lexical-v03-nfkc-casefold-atomic-themes-unicode-LN' AS lexical_token_version
FROM assessed;

ASSERT (SELECT COUNT(*)=COUNT(DISTINCT raw_revision_sha256) FROM `${dataset}.filtered_live_${batch_id}_normalized_v01`)
 AS 'Normalized current-source revisions are not one to one';
ASSERT (SELECT COUNT(*)=COUNT(DISTINCT raw_revision_sha256) FROM `${dataset}.filtered_live_${batch_id}_parsed_v01`)
 AS 'Parsed current-source revisions are not one to one';
ASSERT (SELECT COUNT(*) FROM `${dataset}.filtered_live_${batch_id}_normalized_v01`)=(SELECT COUNT(*) FROM `${dataset}.filtered_live_${batch_id}_parsed_v01`)
 AS 'Parsed and normalized current-source populations differ';
ASSERT (SELECT COUNT(*)=0 FROM `${dataset}.filtered_live_${batch_id}_normalized_v01` AS n LEFT JOIN `${dataset}.filtered_live_${batch_id}_parsed_v01` AS p
 ON p.raw_revision_sha256=n.raw_revision_sha256
 WHERE p.raw_revision_sha256 IS NULL OR (n.admissible AND ARRAY_LENGTH(p.p.parse_reasons) IS DISTINCT FROM 0))
 AS 'Current-source normalization has a missing or invalid parsed revision';
CREATE TABLE `${dataset}.filtered_live_${batch_id}_representatives_v01` CLUSTER BY document_id AS
SELECT n.*, COUNT(*) OVER(PARTITION BY n.document_id) AS sample_admissible_revision_count
FROM `${dataset}.filtered_live_${batch_id}_normalized_v01` AS n
WHERE n.admissible AND ARRAY_LENGTH(n.lex_tokens)>0
QUALIFY ROW_NUMBER() OVER(PARTITION BY n.document_id
 ORDER BY n.observed_at,n.source_revision_key,n.raw_revision_sha256)=1;

CREATE TABLE `${dataset}.filtered_live_${batch_id}_filter_decisions_v01` CLUSTER BY document_id AS
WITH labels AS (
 SELECT r.document_id,r.raw_revision_sha256,r.observed_day,
 ARRAY(SELECT DISTINCT normalized_label FROM (SELECT TRIM(REGEXP_REPLACE(NORMALIZE_AND_CASEFOLD(label,NFKC),r'[\s\p{Z}\x{0085}\x{001C}-\x{001F}]+',' ')) AS normalized_label FROM UNNEST(p.p.themes) AS label) WHERE normalized_label!='' ORDER BY normalized_label) AS themes,
 r.sample_admissible_revision_count
 FROM `${dataset}.filtered_live_${batch_id}_representatives_v01` AS r
 JOIN `${dataset}.filtered_live_${batch_id}_parsed_v01` AS p ON p.raw_revision_sha256=r.raw_revision_sha256
), evaluated AS (
 SELECT document_id,raw_revision_sha256,observed_day,sample_admissible_revision_count,
 ARRAY_LENGTH(themes) AS full_theme_count,`${dataset}.gkg_expanded_rule_ids_v01`(themes) AS matched_rule_ids
 FROM labels
)
SELECT *,ARRAY_LENGTH(matched_rule_ids)>0 AS expanded_rule_selected,
 '5956ed371c5c56eb1a01402236ca70b4ab383b078b7df7bb0e51bffe37f828bc' AS rules_sha256,
 CASE WHEN ARRAY_LENGTH(matched_rule_ids)>0 THEN 'ADMITTED'
      WHEN full_theme_count=0 THEN 'EXCLUDED_NO_THEMES'
      ELSE 'EXCLUDED_NO_MATCH' END AS admission_status
FROM evaluated;
ASSERT (SELECT COUNT(*) FROM `${dataset}.filtered_live_${batch_id}_filter_decisions_v01`)
 =(SELECT COUNT(*) FROM `${dataset}.filtered_live_${batch_id}_representatives_v01`)
 AS 'Every selected canonical revision must have one filter decision';

CREATE VIEW `${dataset}.filtered_live_${batch_id}_admitted_documents_v01` AS
SELECT r.*,f.full_theme_count,f.matched_rule_ids,f.rules_sha256
FROM `${dataset}.filtered_live_${batch_id}_representatives_v01` AS r
JOIN `${dataset}.filtered_live_${batch_id}_filter_decisions_v01` AS f
 ON f.document_id=r.document_id AND f.raw_revision_sha256=r.raw_revision_sha256
WHERE f.expanded_rule_selected AND f.rules_sha256='5956ed371c5c56eb1a01402236ca70b4ab383b078b7df7bb0e51bffe37f828bc';

-- Same canonical identities and ordering as the existing intake, but 1..500 is valid.
ASSERT (SELECT COUNT(*)=COUNT(DISTINCT document_id) FROM `${dataset}.corpus_documents`)
 AS 'Filtered historical document identities must be unique';
ASSERT (SELECT COUNT(*)>0 FROM `${dataset}.filtered_live_${batch_id}_admitted_documents_v01` AS a
 LEFT JOIN `${dataset}.corpus_documents` AS h ON h.document_id=a.document_id WHERE h.document_id IS NULL)
 AS 'No qualifying fresh documents: keep this receipt; select another explicit closed hour';
CREATE TABLE `${dataset}.filtered_live_${batch_id}_query_documents_v01` CLUSTER BY query_request_id,query_document_id AS
WITH fresh AS (
 SELECT a.*,LOWER(TO_HEX(SHA256(CONCAT(@sample_seed,'\ndocument\n',a.document_id)))) AS document_order
 FROM `${dataset}.filtered_live_${batch_id}_admitted_documents_v01` AS a LEFT JOIN `${dataset}.corpus_documents` AS h ON h.document_id=a.document_id
 WHERE h.document_id IS NULL
), numbered AS (
 SELECT f.*,ROW_NUMBER() OVER(ORDER BY f.document_order,f.document_id) AS query_rank FROM fresh AS f
)
SELECT n.document_id AS query_document_id,n.query_rank,LOWER(TO_HEX(SHA256(CONCAT('{"autoTruncate":false,"endpoint":"gemini-embedding-2","outputDimensionality":3072,"representation_version":"gkg-bq-metadata-v03-total-first-complete-labels-6000utf8","task_configuration":"service_default_no_task_parameter"}','\n',n.metadata_sha256)))) AS query_request_id,
 n.observed_at,n.observed_day,n.document_url,n.title,n.metadata_text,n.metadata_sha256,
 n.lex_tokens,n.quality_flags,n.source_record_key,n.source_revision_key,n.raw_revision_sha256,
 n.sample_admissible_revision_count,n.matched_rule_ids,n.rules_sha256
FROM numbered AS n WHERE n.query_rank<=500;
ASSERT (SELECT COUNT(*) BETWEEN 1 AND 500 AND COUNT(DISTINCT query_document_id)=COUNT(*)
 AND MIN(query_rank)=1 AND MAX(query_rank)=COUNT(*)
 AND COUNTIF(observed_at<@window_start OR observed_at>=@window_end OR BYTE_LENGTH(metadata_text)>6000
 OR metadata_sha256 IS DISTINCT FROM LOWER(TO_HEX(SHA256(metadata_text))) OR ARRAY_LENGTH(lex_tokens)=0
 OR ARRAY_LENGTH(matched_rule_ids)=0 OR rules_sha256!='5956ed371c5c56eb1a01402236ca70b4ab383b078b7df7bb0e51bffe37f828bc')=0
 FROM `${dataset}.filtered_live_${batch_id}_query_documents_v01`) AS 'Live query window, representation or admission contract failed';
