CREATE TEMP TABLE bm25_source_summary AS WITH
  history_summary AS
  (SELECT
     COUNT(*) AS row_count,
     COUNT(DISTINCT h.document_id) AS identity_count,
     COUNTIF(h.document_id IS NULL
             OR h.document_id='') AS invalid_rows
   FROM `${dataset}.corpus_documents` AS h),
  length_summary AS
  (SELECT
     COUNT(*) AS row_count,
     COUNT(DISTINCT l.document_id) AS identity_count,
     COUNTIF(l.document_id IS NULL
             OR l.document_id=''
             OR l.document_length IS NULL
             OR l.document_length<=0) AS invalid_rows,
     SUM(l.document_length) AS token_count,
     AVG(CAST(l.document_length AS FLOAT64)) AS average_length
   FROM `${dataset}.lexical_document_lengths` AS l),
  term_summary AS
  (SELECT
     COUNT(*) AS row_count,
     COUNT(DISTINCT t.term) AS identity_count,
     SUM(t.document_frequency) AS posting_count,
     COUNTIF(t.term IS NULL
             OR t.term=''
             OR t.document_frequency IS NULL
             OR t.document_frequency NOT BETWEEN 1 AND 2000000) AS invalid_rows
   FROM `${dataset}.lexical_term_stats` AS t),
  posting_summary AS
  (SELECT
     COUNT(*) AS row_count,
     SUM(p.term_frequency) AS token_count,
     COUNTIF(p.term IS NULL
             OR p.term=''
             OR p.document_id IS NULL
             OR p.document_id=''
             OR p.term_frequency IS NULL
             OR p.term_frequency<=0) AS invalid_rows
   FROM `${dataset}.lexical_postings` AS p),
  posting_duplicates AS
  (SELECT COALESCE(SUM(x.pairs-1), 0) AS duplicates
   FROM
     (SELECT
        p.term AS term,
        p.document_id AS document_id,
        COUNT(*) AS pairs
      FROM `${dataset}.lexical_postings` AS p
      GROUP BY
        p.term,
        p.document_id
      HAVING COUNT(*)!=1) AS x),
  corpus_summary AS
  (SELECT
     COUNT(*) AS row_count,
     MIN(c.total_token_count) AS token_count,
     MIN(c.average_document_length) AS average_length,
     COUNTIF(c.document_count IS DISTINCT
             FROM 2000000
             OR c.k1 IS DISTINCT
             FROM 1.2
             OR c.b IS DISTINCT
             FROM 0.75
             OR c.stats_scope IS DISTINCT
             FROM 'FULL_FROZEN_CORPUS'
             OR c.average_document_length IS NULL
             OR c.average_document_length<=0
             OR IS_NAN(c.average_document_length)
             OR IS_INF(c.average_document_length)) AS invalid_rows
   FROM `${dataset}.lexical_corpus_stats` AS c),
  history_length_errors AS
  (SELECT COUNTIF(h.document_id IS NULL
                  OR l.document_id IS NULL) AS errors
   FROM `${dataset}.corpus_documents` AS h
   FULL OUTER JOIN `${dataset}.lexical_document_lengths` AS l ON l.document_id=h.document_id)
SELECT
  h.row_count AS historical_documents,
  h.identity_count AS distinct_historical_documents,
  l.row_count AS length_documents,
  l.identity_count AS distinct_length_documents,
  t.row_count AS term_rows,
  t.identity_count AS distinct_terms,
  p.row_count AS posting_rows,
  c.row_count AS corpus_stat_rows,
  IF(h.row_count=2000000
     AND h.identity_count=2000000
     AND h.invalid_rows=0, 0, 1) +IF(l.row_count=2000000
                                     AND l.identity_count=2000000
                                     AND l.invalid_rows=0
                                     AND e.errors=0, 0, 1) +IF(t.row_count>0
                                                               AND t.identity_count=t.row_count
                                                               AND t.invalid_rows=0
                                                               AND t.posting_count=p.row_count, 0, 1) +IF(p.row_count>=2000000
                                                                                                          AND p.invalid_rows=0
                                                                                                          AND d.duplicates=0, 0, 1) +IF(c.row_count=1
                                                                                                                                        AND c.invalid_rows=0
                                                                                                                                        AND c.token_count=l.token_count
                                                                                                                                        AND c.token_count=p.token_count
                                                                                                                                        AND ABS(c.average_length-l.average_length)<=1e-9*GREATEST(1.0, ABS(c.average_length)), 0, 1) AS source_contract_errors
FROM history_summary AS h
CROSS JOIN length_summary AS l
CROSS JOIN term_summary AS t
CROSS JOIN posting_summary AS p
CROSS JOIN posting_duplicates AS d
CROSS JOIN corpus_summary AS c
CROSS JOIN history_length_errors AS e
LIMIT 1;

ASSERT
  (SELECT source_contract_errors=0
   FROM bm25_source_summary) AS 'Stable BM25 requires validated new corpus postings, unique keys and singleton statistics';


CREATE TABLE `${dataset}.filtered_stable_bm25_manifest_v01` AS
SELECT
  'gkg-filtered-stable-bm25-v01-r01' AS release,
  'gkg-bm25-stable-decimal-v01-r01' AS scoring_contract,
  @contract_sha256 AS contract_sha256,
  x.historical_documents AS document_count,
  x.term_rows AS term_rows,
  x.posting_rows AS posting_rows,
  CURRENT_TIMESTAMP() AS recorded_at_utc
FROM bm25_source_summary AS x;


CREATE TABLE `${dataset}.filtered_stable_bm25_history_v01` CLUSTER BY historical_key AS
SELECT
  ROW_NUMBER() OVER(
                    ORDER BY x.document_id) AS historical_key,
  x.document_id AS historical_document_id
FROM `${dataset}.corpus_documents` AS x;


CREATE TABLE `${dataset}.filtered_stable_bm25_terms_v01` CLUSTER BY term AS
SELECT
  ROW_NUMBER() OVER(
                    ORDER BY x.term) AS term_key,
  x.term AS term,
  x.document_frequency AS document_frequency
FROM `${dataset}.lexical_term_stats` AS x;


CREATE TABLE `${dataset}.filtered_stable_bm25_weights_v01` CLUSTER BY
  term_key,
  historical_key AS
SELECT
  t.term_key AS term_key,
  h.historical_key AS historical_key,
  CAST(LN(1.0+(c.document_count-t.document_frequency+0.5)/(t.document_frequency+0.5)) *(p.term_frequency*(c.k1+1.0)) / (p.term_frequency+c.k1*(1.0-c.b+c.b*l.document_length/c.average_document_length)) AS BIGNUMERIC) AS term_score_exact
FROM `${dataset}.lexical_postings` AS p
JOIN `${dataset}.filtered_stable_bm25_terms_v01` AS t ON t.term=p.term
JOIN `${dataset}.filtered_stable_bm25_history_v01` AS h ON h.historical_document_id=p.document_id
JOIN `${dataset}.lexical_document_lengths` AS l ON l.document_id=p.document_id
CROSS JOIN `${dataset}.lexical_corpus_stats` AS c;

ASSERT
  (SELECT prepared_contract_errors=0
   FROM
     (WITH history_summary AS
        (SELECT COUNT(*) AS row_count, COUNT(DISTINCT h.historical_key) AS key_count, COUNT(DISTINCT h.historical_document_id) AS identity_count, COUNTIF(h.historical_key IS NULL
                                                                                                                                                          OR h.historical_key NOT BETWEEN 1 AND 2000000
                                                                                                                                                          OR h.historical_document_id IS NULL
                                                                                                                                                          OR h.historical_document_id='') AS invalid_rows
         FROM `${dataset}.filtered_stable_bm25_history_v01` AS h), history_order AS
        (SELECT COUNTIF(x.historical_key IS DISTINCT
                        FROM x.expected_key) AS errors
         FROM
           (SELECT h.historical_key AS historical_key, ROW_NUMBER() OVER(
                                                                         ORDER BY h.historical_document_id) AS expected_key
            FROM `${dataset}.filtered_stable_bm25_history_v01` AS h) AS x), term_summary AS
        (SELECT COUNT(*) AS row_count, COUNT(DISTINCT t.term_key) AS key_count, COUNT(DISTINCT t.term) AS identity_count, COUNTIF(t.term_key IS NULL
                                                                                                                                  OR t.term_key NOT BETWEEN 1 AND
                                                                                                                                    (SELECT m.term_rows
                                                                                                                                     FROM `${dataset}.filtered_stable_bm25_manifest_v01` AS m)
                                                                                                                                  OR t.term IS NULL
                                                                                                                                  OR t.term='') AS invalid_rows
         FROM `${dataset}.filtered_stable_bm25_terms_v01` AS t), weight_summary AS
        (SELECT COUNT(*) AS row_count, COUNT(DISTINCT w.historical_key) AS historical_documents, COUNTIF(w.historical_key IS NULL
                                                                                                         OR w.historical_key NOT BETWEEN 1 AND 2000000
                                                                                                         OR w.term_key IS NULL
                                                                                                         OR w.term_key NOT BETWEEN 1 AND
                                                                                                           (SELECT m.term_rows
                                                                                                            FROM `${dataset}.filtered_stable_bm25_manifest_v01` AS m)
                                                                                                         OR w.term_score_exact IS NULL
                                                                                                         OR w.term_score_exact<=0) AS invalid_rows
         FROM `${dataset}.filtered_stable_bm25_weights_v01` AS w), duplicate_summary AS
        (SELECT COALESCE(SUM(p.pairs-1), 0) AS duplicate_pairs
         FROM
           (SELECT w.term_key AS term_key, w.historical_key AS historical_key, COUNT(*) AS pairs
            FROM `${dataset}.filtered_stable_bm25_weights_v01` AS w
            GROUP BY w.term_key, w.historical_key
            HAVING COUNT(*)!=1) AS p), term_counts AS
        (SELECT w.term_key AS term_key, COUNT(*) AS document_frequency
         FROM `${dataset}.filtered_stable_bm25_weights_v01` AS w
         GROUP BY w.term_key), term_errors AS
        (SELECT COUNTIF(c.term_key IS NULL
                        OR t.term_key IS NULL
                        OR c.document_frequency IS DISTINCT
                        FROM t.document_frequency) AS errors
         FROM `${dataset}.filtered_stable_bm25_terms_v01` AS t
         FULL OUTER JOIN term_counts AS c ON c.term_key=t.term_key), source_count AS
        (SELECT COUNT(*) AS row_count
         FROM `${dataset}.lexical_postings` AS p), cast_errors AS
        (SELECT 0 AS comparisons, 0 AS errors), reference_keys AS
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
            FULL OUTER JOIN `${dataset}.filtered_stable_bm25_terms_v01` AS t ON t.term=s.term) AS errors) SELECT h.row_count AS historical_documents, h.key_count AS distinct_historical_keys, h.identity_count AS distinct_historical_documents, t.row_count AS term_rows, t.key_count AS distinct_term_keys, t.identity_count AS distinct_terms, w.row_count AS weight_rows, p.row_count AS source_posting_rows, w.historical_documents AS weighted_historical_documents, w.invalid_rows AS weight_errors, d.duplicate_pairs AS duplicate_weight_pairs, e.errors AS term_frequency_errors, c.comparisons AS score_cast_comparisons, c.errors AS score_cast_errors, r.errors+o.errors AS reference_key_errors, IF(h.row_count=2000000
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
      LIMIT 1)) AS 'Stable BM25 serving tables must preserve every new-corpus posting and deterministic identity order';
