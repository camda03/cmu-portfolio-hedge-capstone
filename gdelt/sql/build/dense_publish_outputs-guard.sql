ASSERT
  (WITH raw_vectors AS
     (SELECT e.request_id AS request_id, e.batch_number AS batch_number, e.outcome.status AS status, e.outcome.result AS embedding
      FROM `${dataset}.filtered_dense_raw_v01` AS e
      WHERE e.batch_number BETWEEN 0 AND 80), raw_checks AS
     (SELECT r.request_id AS request_id, r.batch_number AS batch_number, r.status AS status, r.embedding AS embedding, ARRAY_LENGTH(r.embedding) AS dimensions,
        (SELECT COUNTIF(x IS NULL
                        OR IS_NAN(x)
                        OR IS_INF(x))
         FROM UNNEST(r.embedding) AS x) AS invalid_components,
        (SELECT MAX(ABS(x))
         FROM UNNEST(r.embedding) AS x) AS scale
      FROM raw_vectors AS r), scaled_norms AS
     (SELECT r.request_id AS request_id, r.batch_number AS batch_number, r.status AS status, r.embedding AS embedding, r.dimensions AS dimensions, r.invalid_components AS invalid_components, IF(r.invalid_components=0
                                                                                                                                                                                                  AND r.scale>0
                                                                                                                                                                                                  AND NOT IS_NAN(r.scale)
                                                                                                                                                                                                  AND NOT IS_INF(r.scale), SAFE_MULTIPLY(r.scale, SQRT(
                                                                                                                                                                                                                                                         (SELECT SUM(POW(SAFE_DIVIDE(x, r.scale), 2))
                                                                                                                                                                                                                                                          FROM UNNEST(r.embedding) AS x))), NULL) AS vector_norm
      FROM raw_checks AS r), validated AS
     (SELECT n.request_id AS request_id, n.batch_number AS batch_number, n.status AS status, n.dimensions AS dimensions, n.invalid_components AS invalid_components, n.vector_norm AS vector_norm, COALESCE(n.status=''
                                                                                                                                                                                                            AND n.dimensions=3072
                                                                                                                                                                                                            AND n.invalid_components=0
                                                                                                                                                                                                            AND n.vector_norm>0
                                                                                                                                                                                                            AND NOT IS_NAN(n.vector_norm)
                                                                                                                                                                                                            AND NOT IS_INF(n.vector_norm), FALSE) AS valid, LOWER(TO_HEX(SHA256(TO_JSON_STRING(n.embedding)))) AS vector_sha256
      FROM scaled_norms AS n), actual AS
     (SELECT v.batch_number AS batch_number, COUNT(*) AS saved_rows, COUNT(DISTINCT v.request_id) AS distinct_requests, COUNTIF(NOT v.valid) AS invalid_outputs
      FROM validated AS v
      GROUP BY v.batch_number) SELECT COUNTIF(b.batch_number IS NULL
                                              OR a.batch_number IS NULL
                                              OR a.saved_rows IS DISTINCT
                                              FROM b.expected_inputs
                                              OR a.distinct_requests IS DISTINCT
                                              FROM b.expected_inputs
                                              OR a.invalid_outputs!=0)=0
   FROM `${dataset}.filtered_dense_batches_v01` AS b
   FULL OUTER JOIN actual AS a ON a.batch_number=b.batch_number) AS 'Every nonempty registered batch needs its complete valid saved output';
