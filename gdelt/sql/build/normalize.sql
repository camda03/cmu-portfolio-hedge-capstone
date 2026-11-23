CREATE OR REPLACE TABLE `${dataset}.normalized_observations` PARTITION BY observed_day CLUSTER BY document_id AS WITH
  parsed AS
  (SELECT
     r.raw_revision_sha256 AS raw_revision_sha256,
     r.batch_month AS batch_month,
     r.candidate_day AS candidate_day,
     r.candidate_order AS candidate_order,
     r.source_rows_for_raw_url_day AS source_rows_for_raw_url_day,
     r.source_record_ids_for_raw_url_day AS source_record_ids_for_raw_url_day,
     r.p AS p
   FROM `${dataset}.parsed_observations` AS r),
  normalized AS
  (SELECT
     raw_revision_sha256,
     batch_month,
     candidate_day,
     candidate_order,
     source_rows_for_raw_url_day,
     source_record_ids_for_raw_url_day,
     p,
     TRIM(REGEXP_REPLACE(NORMALIZE(p.title, NFKC), r'[\s\p{Z}\x{0085}\x{001C}-\x{001F}]+', ' ')) AS title,
     TRIM(REGEXP_REPLACE(NORMALIZE_AND_CASEFOLD(p.title, NFKC), r'[\s\p{Z}\x{0085}\x{001C}-\x{001F}]+', ' ')) AS metadata_title,
     ARRAY
     (SELECT DISTINCT normalized_label
      FROM
        (SELECT TRIM(REGEXP_REPLACE(NORMALIZE_AND_CASEFOLD(label, NFKC), r'[\s\p{Z}\x{0085}\x{001C}-\x{001F}]+', ' ')) AS normalized_label
         FROM UNNEST(p.themes) AS label)
      WHERE normalized_label!=''
      ORDER BY normalized_label) AS themes,
     ARRAY
     (SELECT DISTINCT normalized_label
      FROM
        (SELECT TRIM(REGEXP_REPLACE(NORMALIZE_AND_CASEFOLD(label, NFKC), r'[\s\p{Z}\x{0085}\x{001C}-\x{001F}]+', ' ')) AS normalized_label
         FROM UNNEST(p.persons) AS label)
      WHERE normalized_label!=''
      ORDER BY normalized_label) AS persons,
     ARRAY
     (SELECT DISTINCT normalized_label
      FROM
        (SELECT TRIM(REGEXP_REPLACE(NORMALIZE_AND_CASEFOLD(label, NFKC), r'[\s\p{Z}\x{0085}\x{001C}-\x{001F}]+', ' ')) AS normalized_label
         FROM UNNEST(p.organizations) AS label)
      WHERE normalized_label!=''
      ORDER BY normalized_label) AS organizations,
     ARRAY
     (SELECT DISTINCT normalized_label
      FROM
        (SELECT TRIM(REGEXP_REPLACE(NORMALIZE_AND_CASEFOLD(label, NFKC), r'[\s\p{Z}\x{0085}\x{001C}-\x{001F}]+', ' ')) AS normalized_label
         FROM UNNEST(p.locations) AS label)
      WHERE normalized_label!=''
      ORDER BY normalized_label) AS locations,
     IF(REGEXP_CONTAINS(p.gkg_record_id, r'^[0-9]{14}-T?[0-9]+$'), SAFE.PARSE_TIMESTAMP('%Y%m%d%H%M%S', SUBSTR(p.gkg_record_id, 1, 14), 'UTC'), NULL) AS observed_at,
     IF(REGEXP_CONTAINS(p.gkg_date, r'^[0-9]{14}$'), SAFE.PARSE_TIMESTAMP('%Y%m%d%H%M%S', p.gkg_date, 'UTC'), NULL) AS source_date,
     IF(p.document_identity_json IS NOT NULL, LOWER(TO_HEX(SHA256(CONCAT('gkg-document-v1\n', p.document_identity_json)))), NULL) AS document_id,
     IF(p.source_payload_canonical_json IS NOT NULL, LOWER(TO_HEX(SHA256(CONCAT('gkg-named-payload-v1\n', p.source_payload_canonical_json)))), NULL) AS source_payload_sha256
   FROM parsed),
  with_full_text AS
  (SELECT
     raw_revision_sha256,
     batch_month,
     candidate_day,
     candidate_order,
     source_rows_for_raw_url_day,
     source_record_ids_for_raw_url_day,
     p,
     title,
     metadata_title,
     themes,
     persons,
     organizations,
     locations,
     observed_at,
     source_date,
     document_id,
     source_payload_sha256,
     ARRAY_TO_STRING(ARRAY_CONCAT(IF(metadata_title!='', [CONCAT('title: ',metadata_title)],ARRAY<STRING>[]), IF(ARRAY_LENGTH(themes)>0, [CONCAT('themes: ',ARRAY_TO_STRING(themes,'; '))],ARRAY<STRING>[]), IF(ARRAY_LENGTH(persons)>0, [CONCAT('persons: ',ARRAY_TO_STRING(persons,'; '))],ARRAY<STRING>[]), IF(ARRAY_LENGTH(organizations)>0, [CONCAT('organizations: ',ARRAY_TO_STRING(organizations,'; '))],ARRAY<STRING>[]), IF(ARRAY_LENGTH(locations)>0, [CONCAT('locations: ',ARRAY_TO_STRING(locations,'; '))],ARRAY<STRING>[])), '\n') AS full_metadata_text,
     IF(document_id IS NOT NULL, TO_JSON_STRING(STRUCT(document_id AS document_key, p.gkg_record_id AS gkg_record_id)), NULL) AS source_record_identity_json
   FROM normalized),
  bounded AS
  (SELECT
     raw_revision_sha256,
     batch_month,
     candidate_day,
     candidate_order,
     source_rows_for_raw_url_day,
     source_record_ids_for_raw_url_day,
     p,
     title,
     observed_at,
     source_date,
     document_id,
     source_payload_sha256,
     source_record_identity_json,
     IF(source_record_identity_json IS NOT NULL, LOWER(TO_HEX(SHA256(CONCAT('gkg-source-record-v02-document-scoped\n', source_record_identity_json)))), NULL) AS source_record_key,
     IF(BYTE_LENGTH(full_metadata_text)<=6000, STRUCT(full_metadata_text AS metadata_text, metadata_title AS title, themes AS themes, persons AS persons, organizations AS organizations, locations AS locations, TO_JSON_STRING(STRUCT(6000 AS maximum_utf8_bytes, BYTE_LENGTH(full_metadata_text) AS original_utf8_bytes, BYTE_LENGTH(full_metadata_text) AS retained_utf8_bytes,ARRAY<STRING>[] AS truncated_fields, STRUCT(0 AS title, 0 AS themes, 0 AS persons, 0 AS organizations, 0 AS locations) AS omitted_units, 'full_if_total_fits_else_balanced_complete_label_prefixes_with_redistribution' AS policy)) AS truncation_json), `${dataset}.gkg_bound_metadata_v03`(metadata_title, themes, persons, organizations, locations)) AS b
   FROM with_full_text),
  tokenized AS
  (SELECT
     raw_revision_sha256,
     batch_month,
     candidate_day,
     candidate_order,
     source_rows_for_raw_url_day,
     source_record_ids_for_raw_url_day,
     p,
     title,
     observed_at,
     source_date,
     document_id,
     source_payload_sha256,
     source_record_identity_json,
     source_record_key,
     b,
     IF(source_record_key IS NOT NULL
        AND source_payload_sha256 IS NOT NULL, TO_JSON_STRING([source_record_key,source_payload_sha256]), NULL) AS source_revision_identity_json,
     ARRAY_CONCAT(ARRAY
                    (SELECT CONCAT('theme:', term)
                     FROM UNNEST(b.themes) AS term), ARRAY
                    (SELECT CONCAT('word:', term)
                     FROM UNNEST(REGEXP_EXTRACT_ALL(ARRAY_TO_STRING([b.title,ARRAY_TO_STRING(b.persons,' '),ARRAY_TO_STRING(b.organizations,' '),ARRAY_TO_STRING(b.locations,' ')], ' '), r'[\p{L}\p{N}]+')) AS term)) AS lex_tokens
   FROM bounded),
  assessed AS
  (SELECT
     raw_revision_sha256,
     batch_month,
     candidate_day,
     candidate_order,
     source_rows_for_raw_url_day,
     source_record_ids_for_raw_url_day,
     p,
     title,
     observed_at,
     document_id,
     source_payload_sha256,
     source_record_identity_json,
     source_record_key,
     b,
     source_revision_identity_json,
     lex_tokens,
     ARRAY_CONCAT(p.parse_reasons, IF(p.source_collection IS NULL
                                      OR p.source_collection!='1', ['not_web_source_collection'],ARRAY<STRING>[]), IF(observed_at IS NULL, ['invalid_record_timestamp'],ARRAY<STRING>[]), IF(p.gkg_date='0', ['unknown_source_date'], IF(source_date IS NULL, ['invalid_source_date'], IF(source_date!=observed_at, ['source_date_record_timestamp_conflict'],ARRAY<STRING>[]))), IF(observed_at IS NULL
                                                                                                                                                                                                                                                                                                                                                                                     OR candidate_day IS NULL
                                                                                                                                                                                                                                                                                                                                                                                     OR candidate_day!=FORMAT_DATE('%Y-%m-%d', DATE(observed_at)), ['candidate_day_observation_date_conflict'],ARRAY<STRING>[]), IF(b.metadata_text='', ['empty_metadata'],ARRAY<STRING>[]), IF(ARRAY_LENGTH(lex_tokens)=0, ['zero_lexical_tokens'],ARRAY<STRING>[])) AS rejection_reasons
   FROM tokenized)
SELECT
  raw_revision_sha256,
  batch_month,
  candidate_day,
  candidate_order,
  source_rows_for_raw_url_day,
  source_record_ids_for_raw_url_day,
  document_id,
  source_record_key,
  IF(source_revision_identity_json IS NOT NULL, LOWER(TO_HEX(SHA256(CONCAT('gkg-observation-revision-v1\n', source_revision_identity_json)))), NULL) AS source_revision_key,
  source_payload_sha256,
  p.gkg_record_id AS gkg_record_id,
  p.source_collection AS source_collection,
  p.source_name AS source_name,
  p.document_url AS document_url,
  DATE(observed_at) AS observed_day,
  observed_at,
  title,
  b.metadata_text AS metadata_text,
  LOWER(TO_HEX(SHA256(b.metadata_text))) AS metadata_sha256,
  lex_tokens,
  ARRAY_LENGTH(lex_tokens) AS lexical_token_count,

  (SELECT COUNT(DISTINCT term)
   FROM UNNEST(lex_tokens) AS term) AS lexical_unique_token_count,
  ARRAY_CONCAT(IF(title='', ['title_missing'],ARRAY<STRING>[]), IF(title=''
                                                                   AND (IF(ARRAY_LENGTH(b.themes)>0, 1, 0)+IF(ARRAY_LENGTH(b.persons)>0, 1, 0)+IF(ARRAY_LENGTH(b.organizations)>0, 1, 0)+IF(ARRAY_LENGTH(b.locations)>0, 1, 0))<=1, ['single_feature_family_without_title'],ARRAY<STRING>[]), IF(
                                                                                                                                                                                                                                                                                                   (SELECT COUNT(DISTINCT term)
                                                                                                                                                                                                                                                                                                    FROM UNNEST(lex_tokens) AS term)<10, ['fewer_than_10_distinct_lexical_terms'],ARRAY<STRING>[]), IF(ARRAY_LENGTH(JSON_VALUE_ARRAY(b.truncation_json, '$.truncated_fields'))>0, ['metadata_truncated'],ARRAY<STRING>[]), rejection_reasons) AS quality_flags,
  ARRAY_LENGTH(rejection_reasons)=0 AS admissible,
  rejection_reasons,
  b.truncation_json AS truncation_json,
  p.document_identity_json AS source_document_identity_json,
  source_record_identity_json,
  source_revision_identity_json,
  'gkg-bq-identity-v01-conservative-ascii-host' AS normalization_version,
  'gkg-bq-metadata-v03-total-first-complete-labels-6000utf8' AS representation_version,
  'gkg-bq-lexical-v03-nfkc-casefold-atomic-themes-unicode-LN' AS lexical_token_version
FROM assessed;
