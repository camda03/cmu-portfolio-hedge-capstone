-- Table identifiers are validated template inputs; scalar inputs are typed query parameters.

CREATE TEMP TABLE evaluation_status AS WITH
  queries AS
  (SELECT
     q.query_document_id AS query_document_id,
     q.query_key AS query_key
   FROM UNNEST([STRUCT('q1' AS query_document_id,1 AS query_key),STRUCT('q2' AS query_document_id,1 AS query_key),
 STRUCT('q3' AS query_document_id,2 AS query_key)]) AS q),
  query_terms AS
  (SELECT
     t.query_key AS query_key,
     t.term_key AS term_key
   FROM UNNEST([STRUCT(1 AS query_key,10 AS term_key),STRUCT(1 AS query_key,20 AS term_key),STRUCT(2 AS query_key,30 AS term_key)]) AS t),
  weights AS
  (SELECT
     w.term_key AS term_key,
     w.historical_key AS historical_key,
     w.term_score_exact AS term_score_exact
   FROM UNNEST([STRUCT(10 AS term_key,1 AS historical_key,BIGNUMERIC '1.00000000000000000001' AS term_score_exact),
 STRUCT(20 AS term_key,1 AS historical_key,BIGNUMERIC '2' AS term_score_exact),
 STRUCT(10 AS term_key,2 AS historical_key,BIGNUMERIC '1.00000000000000000001' AS term_score_exact),
 STRUCT(20 AS term_key,2 AS historical_key,BIGNUMERIC '2' AS term_score_exact),
 STRUCT(30 AS term_key,3 AS historical_key,BIGNUMERIC '4' AS term_score_exact),
 STRUCT(99 AS term_key,3 AS historical_key,BIGNUMERIC '1000' AS term_score_exact)]) AS w),
  joined AS
  (SELECT
     q.query_key AS query_key,
     w.historical_key AS historical_key,
     w.term_score_exact AS term_score_exact
   FROM query_terms AS q
   JOIN weights AS w ON w.term_key=q.term_key),
  scored AS
  (SELECT
     j.query_key AS query_key,
     j.historical_key AS historical_key,
     SUM(j.term_score_exact) AS score
   FROM joined AS j
   GROUP BY
     j.query_key,
     j.historical_key),
  ranked AS
  (SELECT
     s.query_key AS query_key,
     s.historical_key AS historical_key,
     s.score AS score,
     ROW_NUMBER() OVER(PARTITION BY s.query_key
                       ORDER BY s.score DESC, s.historical_key) AS match_rank
   FROM scored AS s),
  expanded AS
  (SELECT
     q.query_document_id AS query_document_id,
     r.historical_key AS historical_key,
     r.score AS score,
     r.match_rank AS match_rank
   FROM ranked AS r
   JOIN queries AS q ON q.query_key=r.query_key)
SELECT

  (SELECT COUNT(*)
   FROM queries) AS fixture_queries,

  (SELECT COUNT(DISTINCT q.query_key)
   FROM queries AS q) AS fixture_unique_queries,

  (SELECT COUNT(*)
   FROM weights) AS fixture_postings,

  (SELECT COUNT(*)
   FROM joined) AS fixture_join_rows,

  (SELECT COUNT(*)
   FROM scored) AS fixture_candidate_pairs,
  COUNT(*) AS fixture_output_pairs,
  COUNTIF((e.query_document_id IN ('q1', 'q2')
           AND e.score IS DISTINCT
           FROM BIGNUMERIC '3.00000000000000000001')
          OR (e.query_document_id='q3'
              AND e.score IS DISTINCT
              FROM BIGNUMERIC '4')) AS fixture_score_errors,
  IF(COUNTIF(e.query_document_id='q1')=2
     AND COUNTIF(e.query_document_id='q2')=2
     AND COUNTIF(e.query_document_id='q3')=1, 0, 1) AS fixture_reuse_errors,
  COUNTIF(e.query_document_id IN ('q1', 'q2')
          AND e.match_rank IS DISTINCT
          FROM e.historical_key) AS fixture_tie_errors
FROM expanded AS e
LIMIT 1;

ASSERT
  (SELECT fixture_queries=3
   AND fixture_unique_queries=2
   AND fixture_postings=6
   AND fixture_join_rows=5
   AND fixture_candidate_pairs=3
   AND fixture_output_pairs=5
   AND fixture_score_errors=0
   AND fixture_reuse_errors=0
   AND fixture_tie_errors=0
   FROM evaluation_status) AS 'Repeated lexical queries, exact decimal accumulation and deterministic score ties must agree';


SELECT
  fixture_queries,
  fixture_unique_queries,
  fixture_postings,
  fixture_join_rows,
  fixture_candidate_pairs,
  fixture_output_pairs,
  fixture_score_errors,
  fixture_reuse_errors,
  fixture_tie_errors
FROM evaluation_status
LIMIT 1;
