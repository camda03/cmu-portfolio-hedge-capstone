CREATE TEMP TABLE validation_control AS WITH
  actual AS
  (SELECT
     COUNT(*) AS actual_inputs,
     COUNT(DISTINCT p.request_id) AS distinct_inputs,
     COUNTIF(p.model_input IS NULL
             OR p.model_input=''
             OR BYTE_LENGTH(p.model_input)>6000
             OR p.metadata_sha256 IS DISTINCT
             FROM LOWER(TO_HEX(SHA256(p.model_input)))
             OR p.request_id IS DISTINCT
             FROM LOWER(TO_HEX(SHA256(CONCAT('{"autoTruncate":false,"endpoint":"gemini-embedding-2","outputDimensionality":3072,"representation_version":"gkg-bq-metadata-v03-total-first-complete-labels-6000utf8","task_configuration":"service_default_no_task_parameter"}', '\n', p.metadata_sha256))))
             OR p.cache_source_kind IS DISTINCT
             FROM 0
             OR p.new_input_rank IS NULL
             OR p.new_input_rank NOT BETWEEN 1+(@batch_number-1)*25000 AND @batch_number*25000) AS invalid_inputs,
     MIN(p.new_input_rank) AS first_new_rank,
     MAX(p.new_input_rank) AS last_new_rank,
     LOWER(TO_HEX(SHA256(STRING_AGG(CONCAT(p.request_id, '\t', p.metadata_sha256, '\n'), ''
                                    ORDER BY p.new_input_rank)))) AS batch_sha256
   FROM `${dataset}.filtered_dense_inputs_v01` AS p
   WHERE p.batch_number=@batch_number),
  manifest AS
  (SELECT
     COUNT(*) AS batch_rows,
     MAX(b.expected_inputs) AS expected_inputs,
     MAX(b.input_cap) AS input_cap,
     IF(COUNT(*)=1
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
   WHERE b.batch_number=@batch_number),
  outputs AS
  (SELECT COUNT(*) AS existing_output_rows
   FROM `${dataset}.filtered_dense_raw_v01` AS e
   WHERE e.batch_number=@batch_number)
SELECT
  m.batch_rows AS batch_rows,
  m.expected_inputs AS expected_inputs,
  m.input_cap AS input_cap,
  a.actual_inputs AS actual_inputs,
  a.distinct_inputs AS distinct_inputs,
  a.invalid_inputs AS invalid_inputs,
  m.manifest_errors AS manifest_errors,
  o.existing_output_rows AS existing_output_rows,
  a.first_new_rank AS first_new_rank,
  a.last_new_rank AS last_new_rank
FROM actual AS a
CROSS JOIN manifest AS m
CROSS JOIN outputs AS o
LIMIT 1;

ASSERT
  (SELECT COUNT(*)=1
   AND COUNTIF((batch_rows=1
                AND expected_inputs BETWEEN 1 AND 25000
                AND actual_inputs=expected_inputs
                AND distinct_inputs=expected_inputs
                AND input_cap=expected_inputs
                AND invalid_inputs=0
                AND manifest_errors=0
                AND existing_output_rows=0
                AND first_new_rank=1+(@batch_number-1)*25000
                AND last_new_rank=first_new_rank+expected_inputs-1) IS DISTINCT
               FROM TRUE)=0
   FROM validation_control) AS 'dense_batch_before: frozen contract failed; retain the original job for recovery';


SELECT
  batch_rows,
  expected_inputs,
  input_cap,
  actual_inputs,
  distinct_inputs,
  invalid_inputs,
  manifest_errors,
  existing_output_rows,
  first_new_rank,
  last_new_rank
FROM validation_control;
