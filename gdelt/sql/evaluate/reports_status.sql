-- Build the article matches, date histogram, and forward-return views for this search.
-- The seven-column stored-page contract is unchanged; counts are data driven.
-- Each query contributes one vote split across ALL tied highest RRF candidates.
-- No article text, vectors, result pairs or HTML are downloaded by the runner.

CREATE TEMP TABLE evaluation_status AS WITH
  expected AS
  (SELECT
     COUNT(*) AS query_documents,
     DIV(COUNT(*)+19, 20) AS page_count,

     (SELECT COUNT(*)
      FROM `${dense_documents}`) AS historical_documents
   FROM `${query_documents}`),
  pages AS
  (SELECT
     p.run_id AS run_id,
     p.report_name AS report_name,
     p.page_number AS page_number,
     p.html_text AS html_text,
     p.html_sha256 AS html_sha256,
     p.html_bytes AS html_bytes,
     REGEXP_EXTRACT(p.html_text, r'<script id="report-data" type="application/json">(.*?)</script>') AS payload_json
   FROM `${dataset}.full2m_eval_report_pages_v01` AS p),
  cases AS
  (SELECT
     p.page_number AS page_number,
     c AS case_json
   FROM pages AS p
   CROSS JOIN UNNEST(IFNULL(JSON_QUERY_ARRAY(p.payload_json, '$.cases'),[])) AS c
   WHERE p.report_name='best_matches'),
  case_status AS
  (SELECT
     COUNT(*) AS reported_query_articles,
     COUNT(DISTINCT JSON_VALUE(c.case_json, '$.query.document_id')) AS distinct_reported_query_articles,
     COUNT(DISTINCT JSON_VALUE(c.case_json, '$.query_rank')) AS distinct_reported_query_ranks,
     COUNTIF(SAFE_CAST(JSON_VALUE(c.case_json, '$.query_rank') AS INT64) IS NULL
             OR DIV(SAFE_CAST(JSON_VALUE(c.case_json, '$.query_rank') AS INT64)-1, 20)+1!=c.page_number
             OR JSON_VALUE(c.case_json, '$.query.document_id') IS NULL
             OR q.query_document_id IS NULL
             OR SAFE_CAST(JSON_VALUE(c.case_json, '$.source_query_rank') AS INT64) IS DISTINCT
             FROM q.query_rank
             OR SAFE_CAST(JSON_VALUE(c.case_json, '$.query_rank') AS INT64) NOT BETWEEN 1 AND
               (SELECT query_documents
                FROM expected)
             OR JSON_VALUE(c.case_json, '$.cosine.document_id') IS NULL
             OR JSON_VALUE(c.case_json, '$.fused.document_id') IS NULL) AS report_case_errors
   FROM cases AS c
   LEFT JOIN `${query_documents}` AS q ON q.query_document_id=JSON_VALUE(c.case_json, '$.query.document_id')),
  market_status AS
  (SELECT
     MAX(SAFE_CAST(JSON_VALUE(p.payload_json, '$.voted_queries') AS INT64)) AS market_voted_queries,
     MAX(SAFE_CAST(JSON_VALUE(p.payload_json, '$.total_votes') AS FLOAT64)) AS market_total_votes,
     MAX(ARRAY_LENGTH(JSON_QUERY_ARRAY(p.payload_json, '$.weighted_path'))) AS market_horizons,
     MAX(ARRAY_LENGTH(JSON_QUERY_ARRAY(p.payload_json, '$.date_rows'))) AS market_dates
   FROM pages AS p
   WHERE p.report_name='dates_and_market'),
  market_paths AS
  (SELECT
     SAFE_CAST(JSON_VALUE(x, '$.horizon_sessions') AS INT64) AS horizon,
     SAFE_CAST(JSON_VALUE(x, '$.included_vote_weight') AS FLOAT64) AS included_vote_weight,
     SAFE_CAST(JSON_VALUE(x, '$.weighted_price_return') AS FLOAT64) AS weighted_return
   FROM pages AS p
   CROSS JOIN UNNEST(IFNULL(JSON_QUERY_ARRAY(p.payload_json, '$.weighted_path'),[])) AS x
   WHERE p.report_name='dates_and_market'),
  date_path_status AS
  (SELECT COUNTIF(ARRAY_LENGTH(JSON_QUERY_ARRAY(x, '$.forward_returns')) IS DISTINCT
                  FROM 21
                  OR SAFE_CAST(JSON_VALUE(x, '$.forward_returns[0]') AS FLOAT64) IS DISTINCT
                  FROM 0.0
                  OR JSON_VALUE(x, '$.anchor_date') IS NULL
                  OR JSON_VALUE(x, '$.day20_date') IS NULL) AS date_path_errors
   FROM pages AS p
   CROSS JOIN UNNEST(IFNULL(JSON_QUERY_ARRAY(p.payload_json, '$.date_rows'),[])) AS x
   WHERE p.report_name='dates_and_market'),
  market_path_status AS
  (SELECT
     COUNT(DISTINCT m.horizon) AS distinct_market_horizons,
     MIN(m.horizon) AS first_market_horizon,
     MAX(m.horizon) AS last_market_horizon,
     COUNTIF(m.horizon IS NULL
             OR m.horizon<0
             OR m.horizon>20
             OR m.included_vote_weight IS NULL
             OR ABS(m.included_vote_weight-
                      (SELECT query_documents
                       FROM expected))>0.000001
             OR m.weighted_return IS NULL
             OR IS_NAN(m.weighted_return)
             OR IS_INF(m.weighted_return)) AS market_path_errors
   FROM market_paths AS m),
  page_status AS
  (SELECT
     COUNT(*) AS report_pages,
     COUNT(DISTINCT CONCAT(p.report_name, '/', CAST(p.page_number AS STRING))) AS distinct_report_pages,
     COUNTIF(p.report_name='best_matches') AS best_match_pages,
     COUNTIF(p.report_name='dates_and_market') AS market_pages,
     COUNTIF(p.report_name='performance') AS performance_pages,
     COUNTIF(p.run_id IS DISTINCT
             FROM @run_id
             OR p.payload_json IS NULL
             OR JSON_VALUE(p.payload_json, '$.run_id') IS DISTINCT
             FROM @run_id
             OR SAFE_CAST(JSON_VALUE(p.payload_json, '$.page_number') AS INT64) IS DISTINCT
             FROM p.page_number
             OR SAFE_CAST(JSON_VALUE(p.payload_json, '$.query_documents') AS INT64) IS DISTINCT
             FROM
               (SELECT query_documents
                FROM expected)
             OR SAFE_CAST(JSON_VALUE(p.payload_json, '$.historical_documents') AS INT64) IS DISTINCT
             FROM
               (SELECT historical_documents
                FROM expected)
             OR SAFE_CAST(JSON_VALUE(p.payload_json, '$.page_count') AS INT64) IS DISTINCT
             FROM
               (SELECT page_count
                FROM expected)
             OR p.report_name NOT IN ('best_matches', 'dates_and_market', 'performance')
             OR p.page_number IS NULL
             OR p.page_number<1
             OR p.page_number>IF(p.report_name='best_matches',
                                   (SELECT page_count
                                    FROM expected),1)) AS report_identity_errors,
     COUNTIF(p.html_text IS NULL
             OR p.html_bytes IS NULL
             OR p.html_bytes<1
             OR p.html_bytes>2097152
             OR p.html_bytes!=BYTE_LENGTH(p.html_text)
             OR p.html_sha256 IS DISTINCT
             FROM LOWER(TO_HEX(SHA256(p.html_text)))) AS report_content_errors,
     SUM(p.html_bytes) AS total_report_html_bytes,
     MAX(p.html_bytes) AS maximum_report_page_bytes
   FROM pages AS p)
SELECT
  p.report_pages AS report_pages,
  p.distinct_report_pages AS distinct_report_pages,
  p.best_match_pages AS best_match_pages,
  p.market_pages AS market_pages,
  p.performance_pages AS performance_pages,
  p.report_identity_errors AS report_identity_errors,
  p.report_content_errors AS report_content_errors,
  p.total_report_html_bytes AS total_report_html_bytes,
  p.maximum_report_page_bytes AS maximum_report_page_bytes,
  c.reported_query_articles AS reported_query_articles,
  c.distinct_reported_query_articles AS distinct_reported_query_articles,
  c.distinct_reported_query_ranks AS distinct_reported_query_ranks,
  c.report_case_errors AS report_case_errors,
  m.market_voted_queries AS market_voted_queries,
  m.market_total_votes AS market_total_votes,
  m.market_horizons AS market_horizons,
  m.market_dates AS market_dates,
  h.distinct_market_horizons AS distinct_market_horizons,
  h.first_market_horizon AS first_market_horizon,
  h.last_market_horizon AS last_market_horizon,
  h.market_path_errors+dp.date_path_errors AS market_path_errors
FROM page_status AS p
CROSS JOIN case_status AS c
CROSS JOIN market_status AS m
CROSS JOIN market_path_status AS h
CROSS JOIN date_path_status AS dp
LIMIT 1;

ASSERT
  (SELECT report_pages=DIV(
                             (SELECT COUNT(*)
                              FROM `${query_documents}`)+19, 20)+2
   AND distinct_report_pages=report_pages
   AND best_match_pages=report_pages-2
   AND market_pages=1
   AND performance_pages=1
   AND report_identity_errors=0
   AND report_content_errors=0
   AND total_report_html_bytes BETWEEN 1 AND 33554432
   AND maximum_report_page_bytes BETWEEN 1 AND 2097152
   AND reported_query_articles=
     (SELECT COUNT(*)
      FROM `${query_documents}`)
   AND distinct_reported_query_articles=reported_query_articles
   AND distinct_reported_query_ranks=reported_query_articles
   AND report_case_errors=0
   AND market_voted_queries=reported_query_articles
   AND ABS(market_total_votes-reported_query_articles)<=0.000001
   AND market_horizons=21
   AND market_dates>0
   AND distinct_market_horizons=21
   AND first_market_horizon=0
   AND last_market_horizon=20
   AND market_path_errors=0
   FROM evaluation_status) AS 'All saved queries and complete vote-weighted paths must be represented in bounded, sealed report pages';


SELECT
  report_pages,
  distinct_report_pages,
  best_match_pages,
  market_pages,
  performance_pages,
  report_identity_errors,
  report_content_errors,
  total_report_html_bytes,
  maximum_report_page_bytes,
  reported_query_articles,
  distinct_reported_query_articles,
  distinct_reported_query_ranks,
  report_case_errors,
  market_voted_queries,
  market_total_votes,
  market_horizons,
  market_dates,
  distinct_market_horizons,
  first_market_horizon,
  last_market_horizon,
  market_path_errors
FROM evaluation_status
LIMIT 1;
