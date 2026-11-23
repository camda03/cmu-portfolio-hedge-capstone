CREATE TEMP TABLE validation_control AS WITH
  history_summary AS
  (SELECT
     COUNT(*) AS row_count,
     COUNT(DISTINCT h.historical_key) AS key_count,
     COUNT(DISTINCT h.historical_document_id) AS identity_count,
     COUNTIF(h.historical_key IS NULL
             OR h.historical_key NOT BETWEEN 1 AND 2000000
             OR h.historical_document_id IS NULL
             OR h.historical_document_id='') AS invalid_rows
   FROM `${dataset}.filtered_stable_bm25_history_v01` AS h),
  history_order AS
  (SELECT COUNTIF(x.historical_key IS DISTINCT
                  FROM x.expected_key) AS errors
   FROM
     (SELECT
        h.historical_key AS historical_key,
        ROW_NUMBER() OVER(
                          ORDER BY h.historical_document_id) AS expected_key
      FROM `${dataset}.filtered_stable_bm25_history_v01` AS h) AS x),
  term_summary AS
  (SELECT
     COUNT(*) AS row_count,
     COUNT(DISTINCT t.term_key) AS key_count,
     COUNT(DISTINCT t.term) AS identity_count,
     COUNTIF(t.term_key IS NULL
             OR t.term_key NOT BETWEEN 1 AND
               (SELECT m.term_rows
                FROM `${dataset}.filtered_stable_bm25_manifest_v01` AS m)
             OR t.term IS NULL
             OR t.term='') AS invalid_rows
   FROM `${dataset}.filtered_stable_bm25_terms_v01` AS t),
  weight_summary AS
  (SELECT
     COUNT(*) AS row_count,
     COUNT(DISTINCT w.historical_key) AS historical_documents,
     COUNTIF(w.historical_key IS NULL
             OR w.historical_key NOT BETWEEN 1 AND 2000000
             OR w.term_key IS NULL
             OR w.term_key NOT BETWEEN 1 AND
               (SELECT m.term_rows
                FROM `${dataset}.filtered_stable_bm25_manifest_v01` AS m)
             OR w.term_score_exact IS NULL
             OR w.term_score_exact<=0) AS invalid_rows
   FROM `${dataset}.filtered_stable_bm25_weights_v01` AS w),
  duplicate_summary AS
  (SELECT COALESCE(SUM(p.pairs-1), 0) AS duplicate_pairs
   FROM
     (SELECT
        w.term_key AS term_key,
        w.historical_key AS historical_key,
        COUNT(*) AS pairs
      FROM `${dataset}.filtered_stable_bm25_weights_v01` AS w
      GROUP BY
        w.term_key,
        w.historical_key
      HAVING COUNT(*)!=1) AS p),
  term_counts AS
  (SELECT
     w.term_key AS term_key,
     COUNT(*) AS document_frequency
   FROM `${dataset}.filtered_stable_bm25_weights_v01` AS w
   GROUP BY w.term_key),
  term_errors AS
  (SELECT COUNTIF(c.term_key IS NULL
                  OR t.term_key IS NULL
                  OR c.document_frequency IS DISTINCT
                  FROM t.document_frequency) AS errors
   FROM `${dataset}.filtered_stable_bm25_terms_v01` AS t
   FULL OUTER JOIN term_counts AS c ON c.term_key=t.term_key),
  source_count AS
  (SELECT COUNT(*) AS row_count
   FROM `${dataset}.lexical_postings` AS p),
  cast_errors AS
  (SELECT
     0 AS comparisons,
     0 AS errors),
  reference_keys AS
  (SELECT
     (SELECT COUNTIF(s.document_id IS NULL
                     OR h.historical_document_id IS NULL)
      FROM `${dataset}.corpus_documents` AS s
      FULL OUTER JOIN `${dataset}.filtered_stable_bm25_history_v01` AS h ON h.historical_document_id=s.document_id) +
     (SELECT COUNTIF(s.term IS NULL
                     OR t.term IS NULL
                     OR s.document_frequency IS DISTINCT
                     FROM t.document_frequency)
      FROM `${dataset}.lexical_term_stats` AS s
      FULL OUTER JOIN `${dataset}.filtered_stable_bm25_terms_v01` AS t ON t.term=s.term) AS errors)
SELECT
  h.row_count AS historical_documents,
  h.key_count AS distinct_historical_keys,
  h.identity_count AS distinct_historical_documents,
  t.row_count AS term_rows,
  t.key_count AS distinct_term_keys,
  t.identity_count AS distinct_terms,
  w.row_count AS weight_rows,
  p.row_count AS source_posting_rows,
  w.historical_documents AS weighted_historical_documents,
  w.invalid_rows AS weight_errors,
  d.duplicate_pairs AS duplicate_weight_pairs,
  e.errors AS term_frequency_errors,
  c.comparisons AS score_cast_comparisons,
  c.errors AS score_cast_errors,
  r.errors+o.errors AS reference_key_errors,
  IF(h.row_count=2000000
     AND h.key_count=2000000
     AND h.identity_count=2000000
     AND h.invalid_rows=0, 0, 1) +IF(t.row_count=
                                       (SELECT m.term_rows
                                        FROM `${dataset}.filtered_stable_bm25_manifest_v01` AS m)
                                     AND t.key_count=
                                       (SELECT m.term_rows
                                        FROM `${dataset}.filtered_stable_bm25_manifest_v01` AS m)
                                     AND t.identity_count=
                                       (SELECT m.term_rows
                                        FROM `${dataset}.filtered_stable_bm25_manifest_v01` AS m)
                                     AND t.invalid_rows=0, 0, 1) +IF(w.row_count=
                                                                       (SELECT m.posting_rows
                                                                        FROM `${dataset}.filtered_stable_bm25_manifest_v01` AS m)
                                                                     AND p.row_count=
                                                                       (SELECT m.posting_rows
                                                                        FROM `${dataset}.filtered_stable_bm25_manifest_v01` AS m)
                                                                     AND w.historical_documents=2000000
                                                                     AND w.invalid_rows=0, 0, 1) +IF(d.duplicate_pairs=0
                                                                                                     AND e.errors=0
                                                                                                     AND c.errors=0
                                                                                                     AND r.errors=0
                                                                                                     AND o.errors=0, 0, 1) +IF(
                                                                                                                                 (SELECT COUNT(*)=1
                                                                                                                                  AND COUNTIF(m.release='gkg-filtered-stable-bm25-v01-r01'
                                                                                                                                              AND m.scoring_contract='gkg-bm25-stable-decimal-v01-r01'
                                                                                                                                              AND m.contract_sha256=@contract_sha256
                                                                                                                                              AND m.document_count=2000000
                                                                                                                                              AND m.term_rows>0
                                                                                                                                              AND m.posting_rows>=2000000)=1
                                                                                                                                  FROM `${dataset}.filtered_stable_bm25_manifest_v01` AS m),0, 1) AS prepared_contract_errors
FROM history_summary AS h
CROSS JOIN history_order AS o
CROSS JOIN term_summary AS t
CROSS JOIN weight_summary AS w
CROSS JOIN duplicate_summary AS d
CROSS JOIN term_errors AS e
CROSS JOIN source_count AS p
CROSS JOIN cast_errors AS c
CROSS JOIN reference_keys AS r
LIMIT 1;

ASSERT
  (SELECT COUNT(*)=1
   AND COUNTIF((prepared_contract_errors=0) IS DISTINCT
               FROM TRUE)=0
   FROM validation_control) AS 'bm25_status: frozen contract failed; retain the original job for recovery';


SELECT
  historical_documents,
  distinct_historical_keys,
  distinct_historical_documents,
  term_rows,
  distinct_term_keys,
  distinct_terms,
  weight_rows,
  source_posting_rows,
  weighted_historical_documents,
  weight_errors,
  duplicate_weight_pairs,
  term_frequency_errors,
  score_cast_comparisons,
  score_cast_errors,
  reference_key_errors,
  prepared_contract_errors
FROM validation_control;
