CREATE TEMP TABLE validation_control AS WITH
  expected AS
  (SELECT
     p.request_id AS request_id,
     p.batch_number AS batch_number
   FROM `${dataset}.filtered_dense_inputs_v01` AS p
   WHERE p.batch_number=@batch_number),
  raw_vectors AS
  (SELECT
     e.request_id AS request_id,
     e.batch_number AS batch_number,
     e.outcome.status AS status,
     e.outcome.result AS embedding
   FROM `${dataset}.filtered_dense_raw_v01` AS e
   WHERE e.batch_number=@batch_number),
  raw_checks AS
  (SELECT
     r.request_id AS request_id,
     r.batch_number AS batch_number,
     r.status AS status,
     r.embedding AS embedding,
     ARRAY_LENGTH(r.embedding) AS dimensions,

     (SELECT COUNTIF(x IS NULL
                     OR IS_NAN(x)
                     OR IS_INF(x))
      FROM UNNEST(r.embedding) AS x) AS invalid_components,

     (SELECT MAX(ABS(x))
      FROM UNNEST(r.embedding) AS x) AS scale
   FROM raw_vectors AS r),
  scaled_norms AS
  (SELECT
     r.request_id AS request_id,
     r.batch_number AS batch_number,
     r.status AS status,
     r.embedding AS embedding,
     r.dimensions AS dimensions,
     r.invalid_components AS invalid_components,
     IF(r.invalid_components=0
        AND r.scale>0
        AND NOT IS_NAN(r.scale)
        AND NOT IS_INF(r.scale), SAFE_MULTIPLY(r.scale, SQRT(
                                                               (SELECT SUM(POW(SAFE_DIVIDE(x, r.scale), 2))
                                                                FROM UNNEST(r.embedding) AS x))), NULL) AS vector_norm
   FROM raw_checks AS r),
  validated AS
  (SELECT
     n.request_id AS request_id,
     n.batch_number AS batch_number,
     n.status AS status,
     n.dimensions AS dimensions,
     n.invalid_components AS invalid_components,
     n.vector_norm AS vector_norm,
     COALESCE(n.status=''
              AND n.dimensions=3072
              AND n.invalid_components=0
              AND n.vector_norm>0
              AND NOT IS_NAN(n.vector_norm)
              AND NOT IS_INF(n.vector_norm), FALSE) AS valid,
     LOWER(TO_HEX(SHA256(TO_JSON_STRING(n.embedding)))) AS vector_sha256
   FROM scaled_norms AS n),
  totals AS
  (SELECT
     COUNT(*) AS saved_rows,
     COUNT(DISTINCT v.request_id) AS distinct_saved_requests,
     COUNT(*)-COUNT(DISTINCT v.request_id) AS duplicate_outputs,
     COUNTIF(v.valid) AS valid_outputs,
     COUNTIF(v.status IS NULL
             OR v.status!='') AS invalid_status_outputs,
     COUNTIF(v.dimensions IS NULL
             OR v.dimensions!=3072) AS wrong_dimension_outputs,
     COUNTIF(v.invalid_components>0) AS nonfinite_component_outputs,
     COUNTIF(v.vector_norm IS NULL
             OR v.vector_norm<=0
             OR IS_NAN(v.vector_norm)
             OR IS_INF(v.vector_norm)) AS invalid_norm_outputs,
     COUNT(DISTINCT IF(v.valid, v.vector_sha256, NULL)) AS distinct_valid_vectors,
     MIN(IF(v.valid, v.vector_norm, NULL)) AS minimum_vector_norm,
     MAX(IF(v.valid, v.vector_norm, NULL)) AS maximum_vector_norm,
     COALESCE(SUM(COALESCE(v.dimensions, 0)), 0) AS saved_vector_components
   FROM validated AS v),
  missing AS
  (SELECT COUNTIF(v.request_id IS NULL) AS missing_outputs
   FROM expected AS e
   LEFT JOIN validated AS v ON v.request_id=e.request_id
   AND v.batch_number=e.batch_number),
  extras AS
  (SELECT COUNTIF(e.request_id IS NULL) AS unexpected_outputs
   FROM validated AS v
   LEFT JOIN expected AS e ON e.request_id=v.request_id
   AND e.batch_number=v.batch_number),
  expected_total AS
  (SELECT COUNT(*) AS expected_inputs
   FROM expected),
  batch AS
  (WITH
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
      WHERE e.batch_number=@batch_number) SELECT
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
   LIMIT 1)
SELECT
  e.expected_inputs AS expected_inputs,
  t.saved_rows AS saved_rows,
  t.distinct_saved_requests AS distinct_saved_requests,
  m.missing_outputs AS missing_outputs,
  x.unexpected_outputs AS unexpected_outputs,
  t.duplicate_outputs AS duplicate_outputs,
  t.valid_outputs AS valid_outputs,
  t.invalid_status_outputs AS invalid_status_outputs,
  t.wrong_dimension_outputs AS wrong_dimension_outputs,
  t.nonfinite_component_outputs AS nonfinite_component_outputs,
  t.invalid_norm_outputs AS invalid_norm_outputs,
  t.distinct_valid_vectors AS distinct_valid_vectors,
  t.minimum_vector_norm AS minimum_vector_norm,
  t.maximum_vector_norm AS maximum_vector_norm,
  t.saved_vector_components AS saved_vector_components,
  b.manifest_errors AS batch_manifest_errors
FROM expected_total AS e
CROSS JOIN totals AS t
CROSS JOIN missing AS m
CROSS JOIN extras AS x
CROSS JOIN batch AS b
LIMIT 1;

ASSERT
  (SELECT COUNT(*)=1
   AND COUNTIF((expected_inputs BETWEEN 1 AND 25000
                AND saved_rows=expected_inputs
                AND distinct_saved_requests=expected_inputs
                AND valid_outputs=expected_inputs
                AND missing_outputs=0
                AND unexpected_outputs=0
                AND duplicate_outputs=0
                AND invalid_status_outputs=0
                AND wrong_dimension_outputs=0
                AND nonfinite_component_outputs=0
                AND invalid_norm_outputs=0
                AND batch_manifest_errors=0
                AND saved_vector_components=expected_inputs*3072) IS DISTINCT
               FROM TRUE)=0
   FROM validation_control) AS 'dense_batch_after: frozen contract failed; retain the original job for recovery';


SELECT
  expected_inputs,
  saved_rows,
  distinct_saved_requests,
  missing_outputs,
  unexpected_outputs,
  duplicate_outputs,
  valid_outputs,
  invalid_status_outputs,
  wrong_dimension_outputs,
  nonfinite_component_outputs,
  invalid_norm_outputs,
  distinct_valid_vectors,
  minimum_vector_norm,
  maximum_vector_norm,
  saved_vector_components,
  batch_manifest_errors
FROM validation_control;
