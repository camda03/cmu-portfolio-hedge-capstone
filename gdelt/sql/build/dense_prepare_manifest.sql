CREATE TABLE `${dataset}.filtered_dense_manifest_v01` AS WITH
  input_blocks AS
  (SELECT
     DIV(p.full_input_rank-1, 1000) AS rank_block,
     COUNT(*) AS input_count,
     COUNTIF(p.cache_source_kind=1) AS cached_inputs,
     COUNTIF(p.cache_source_kind=0) AS new_inputs,
     LOWER(TO_HEX(SHA256(STRING_AGG(CONCAT(CAST(p.full_input_rank AS STRING), '\t', p.request_id, '\t', p.metadata_sha256, '\t', CAST(p.batch_number AS STRING), '\n'), ''
                                    ORDER BY p.full_input_rank)))) AS block_sha256
   FROM `${dataset}.filtered_dense_inputs_v01` AS p
   WHERE p.batch_number BETWEEN 0 AND 80
   GROUP BY rank_block),
  input_fingerprint AS
  (SELECT
     COALESCE(SUM(b.input_count), 0) AS input_count,
     COALESCE(SUM(b.cached_inputs), 0) AS cached_inputs,
     COALESCE(SUM(b.new_inputs), 0) AS new_inputs,
     LOWER(TO_HEX(SHA256(COALESCE(STRING_AGG(CONCAT(CAST(b.rank_block AS STRING), '\t', b.block_sha256, '\n'), ''
                                             ORDER BY b.rank_block), '')))) AS inputs_sha256
   FROM input_blocks AS b),
  document_blocks AS
  (SELECT
     DIV(m.corpus_rank-1, 1000) AS rank_block,
     COUNT(*) AS document_count,
     LOWER(TO_HEX(SHA256(STRING_AGG(CONCAT(CAST(m.corpus_rank AS STRING), '\t', m.document_id, '\t', m.request_id, '\t', CAST(m.observed_day AS STRING), '\n'), ''
                                    ORDER BY m.corpus_rank)))) AS block_sha256
   FROM `${dataset}.filtered_dense_documents_v01` AS m
   GROUP BY rank_block),
  document_fingerprint AS
  (SELECT
     COALESCE(SUM(b.document_count), 0) AS document_count,
     LOWER(TO_HEX(SHA256(COALESCE(STRING_AGG(CONCAT(CAST(b.rank_block AS STRING), '\t', b.block_sha256, '\n'), ''
                                             ORDER BY b.rank_block), '')))) AS documents_sha256
   FROM document_blocks AS b),
  cache_blocks AS
  (SELECT
     SUBSTR(k.request_id, 1, 3) AS hash_block,
     COUNT(*) AS cache_count,
     LOWER(TO_HEX(SHA256(STRING_AGG(CONCAT(k.request_id, '\t', k.vector_sha256, '\n'), ''
                                    ORDER BY k.request_id)))) AS block_sha256
   FROM `${dataset}.filtered_dense_cache_v01` AS k
   GROUP BY hash_block),
  cache_fingerprint AS
  (SELECT
     COALESCE(SUM(b.cache_count), 0) AS cache_count,
     LOWER(TO_HEX(SHA256(COALESCE(STRING_AGG(CONCAT(b.hash_block, '\t', b.block_sha256, '\n'), ''
                                             ORDER BY b.hash_block), '')))) AS cache_sha256
   FROM cache_blocks AS b)
SELECT
  'gkg-filtered-2m-dense-v01-r01' AS release,
  @contract_sha256 AS contract_sha256,
  i.input_count AS input_count,
  i.cached_inputs AS cached_inputs,
  i.new_inputs AS new_inputs,
  d.document_count AS document_count,
  25000 AS batch_size,
  DIV(i.new_inputs+25000-1, 25000) AS batch_count,
  i.inputs_sha256 AS inputs_sha256,
  d.documents_sha256 AS documents_sha256,
  k.cache_sha256 AS cache_sha256,
  '{"autoTruncate":false,"endpoint":"gemini-embedding-2","outputDimensionality":3072,"representation_version":"gkg-bq-metadata-v03-total-first-complete-labels-6000utf8","task_configuration":"service_default_no_task_parameter"}' AS embedding_spec_json,
  'b935834ce400c38516af947df67c356c90f67bac60e0feb4e7ee4760c302e9f8' AS embedding_spec_sha256,
  CURRENT_TIMESTAMP() AS recorded_at_utc
FROM input_fingerprint AS i
CROSS JOIN document_fingerprint AS d
CROSS JOIN cache_fingerprint AS k;
