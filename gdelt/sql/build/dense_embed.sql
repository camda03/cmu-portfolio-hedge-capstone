ASSERT
  (SELECT b.batch_rows=1
   AND b.expected_inputs BETWEEN 1 AND b.input_cap
   AND b.input_cap<=25000
   AND b.actual_inputs=b.expected_inputs
   AND b.distinct_inputs=b.expected_inputs
   AND b.invalid_inputs=0
   AND b.manifest_errors=0
   AND b.existing_output_rows=0
   FROM
     (WITH actual AS
        (SELECT COUNT(*) AS actual_inputs, COUNT(DISTINCT p.request_id) AS distinct_inputs, COUNTIF(p.model_input IS NULL
                                                                                                    OR p.model_input=''
                                                                                                    OR BYTE_LENGTH(p.model_input)>6000
                                                                                                    OR p.metadata_sha256 IS DISTINCT
                                                                                                    FROM LOWER(TO_HEX(SHA256(p.model_input)))
                                                                                                    OR p.request_id IS DISTINCT
                                                                                                    FROM LOWER(TO_HEX(SHA256(CONCAT('{"autoTruncate":false,"endpoint":"gemini-embedding-2","outputDimensionality":3072,"representation_version":"gkg-bq-metadata-v03-total-first-complete-labels-6000utf8","task_configuration":"service_default_no_task_parameter"}', '\n', p.metadata_sha256))))
                                                                                                    OR p.cache_source_kind IS DISTINCT
                                                                                                    FROM 0
                                                                                                    OR p.new_input_rank IS NULL
                                                                                                    OR p.new_input_rank NOT BETWEEN 1+(@batch_number-1)*25000 AND @batch_number*25000) AS invalid_inputs, MIN(p.new_input_rank) AS first_new_rank, MAX(p.new_input_rank) AS last_new_rank, LOWER(TO_HEX(SHA256(STRING_AGG(CONCAT(p.request_id, '\t', p.metadata_sha256, '\n'), ''
                                                                                                                                                                                                                                                                                                                          ORDER BY p.new_input_rank)))) AS batch_sha256
         FROM `${dataset}.filtered_dense_inputs_v01` AS p
         WHERE p.batch_number=@batch_number), manifest AS
        (SELECT COUNT(*) AS batch_rows, MAX(b.expected_inputs) AS expected_inputs, MAX(b.input_cap) AS input_cap, IF(COUNT(*)=1
                                                                                                                     AND COUNTIF(b.input_cap=b.expected_inputs
                                                                                                                                 AND b.expected_inputs BETWEEN 1 AND 25000
                                                                                                                                 AND b.expected_inputs=a.actual_inputs
                                                                                                                                 AND a.distinct_inputs=a.actual_inputs
                                                                                                                                 AND a.invalid_inputs=0
                                                                                                                                 AND b.batch_sha256=a.batch_sha256
                                                                                                                                 AND b.first_new_rank=a.first_new_rank
                                                                                                                                 AND b.last_new_rank=a.last_new_rank
                                                                                                                                 AND b.first_new_rank=1+(@batch_number-1)*25000
                                                                                                                                 AND b.last_new_rank=b.first_new_rank+b.expected_inputs-1)=1, 0, 1) AS manifest_errors
         FROM `${dataset}.filtered_dense_batches_v01` AS b
         CROSS JOIN actual AS a
         WHERE b.batch_number=@batch_number), outputs AS
        (SELECT COUNT(*) AS existing_output_rows
         FROM `${dataset}.filtered_dense_raw_v01` AS e
         WHERE e.batch_number=@batch_number) SELECT m.batch_rows AS batch_rows, m.expected_inputs AS expected_inputs, m.input_cap AS input_cap, a.actual_inputs AS actual_inputs, a.distinct_inputs AS distinct_inputs, a.invalid_inputs AS invalid_inputs, m.manifest_errors AS manifest_errors, o.existing_output_rows AS existing_output_rows, a.first_new_rank AS first_new_rank, a.last_new_rank AS last_new_rank
      FROM actual AS a
      CROSS JOIN manifest AS m
      CROSS JOIN outputs AS o
      LIMIT 1) AS b) AS 'Frozen batch changed or outputs exist; recover the original job without repeating inference';

ASSERT
  (SELECT COUNT(*)=1
   AND COUNTIF(m.contract_sha256=@contract_sha256
               AND m.embedding_spec_sha256='b935834ce400c38516af947df67c356c90f67bac60e0feb4e7ee4760c302e9f8'
               AND m.batch_size=25000
               AND @batch_number BETWEEN 1 AND m.batch_count)=1
   FROM `${dataset}.filtered_dense_manifest_v01` AS m) AS 'Only one registered new filtered build batch is permitted';


INSERT INTO `${dataset}.filtered_dense_raw_v01` (batch_number, request_id, outcome, recorded_at_utc)
SELECT
  p.batch_number AS batch_number,
  p.request_id AS request_id,
  AI.EMBED(p.model_input, endpoint=>'gemini-embedding-2', model_params=>JSON '{"outputDimensionality":3072,"autoTruncate":false}') AS outcome,
  CURRENT_TIMESTAMP() AS recorded_at_utc
FROM `${dataset}.filtered_dense_inputs_v01` AS p
WHERE p.batch_number=@batch_number
  AND p.cache_source_kind=0;
