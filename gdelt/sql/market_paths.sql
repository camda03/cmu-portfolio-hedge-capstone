-- T0 is the first trading close strictly after the historical GKG observation date.

CREATE OR REPLACE TABLE `${dataset}.market_paths`
CLUSTER BY observed_day AS
WITH sessions AS (
 SELECT m.session_date AS session_date,m.close_price AS close_price,
 ROW_NUMBER() OVER (ORDER BY m.session_date) AS session_index
 FROM `${dataset}.market_closes` AS m
), anchors AS (
 SELECT q.observed_day AS observed_day,MIN(s.session_index) AS anchor_index
 FROM `${dataset}.daily_quotas` AS q JOIN sessions AS s ON s.session_date>q.observed_day
 GROUP BY q.observed_day
), paths AS (
 SELECT a.observed_day AS observed_day,
 ARRAY_AGG(STRUCT(s.session_index-a.anchor_index AS horizon_sessions,
 s.session_date AS session_date,s.close_price AS close_price) ORDER BY s.session_index) AS observed_path
 FROM anchors AS a JOIN sessions AS s
 ON s.session_index BETWEEN a.anchor_index AND a.anchor_index+20
 GROUP BY a.observed_day
)
SELECT p.observed_day AS observed_day,p.observed_path[SAFE_OFFSET(0)].session_date AS anchor_date,
 p.observed_path[SAFE_OFFSET(20)].session_date AS day20_date,
 ARRAY_LENGTH(p.observed_path) AS price_observations,
 ARRAY_LENGTH(p.observed_path)=21 AND TIMESTAMP_ADD(TIMESTAMP(p.observed_day),INTERVAL 1 DAY)<=TIMESTAMP(@as_of_utc) AS eligible,
 ARRAY(SELECT AS STRUCT x.horizon_sessions AS horizon_sessions,x.session_date AS session_date,
 x.close_price AS close_price,SAFE_DIVIDE(x.close_price,p.observed_path[SAFE_OFFSET(0)].close_price)-1 AS price_return
 FROM UNNEST(p.observed_path) AS x ORDER BY x.horizon_sessions) AS forward_path
FROM paths AS p;

-- Market projection from the stored report query. Tied highest-scoring matches split a single vote.

CREATE TEMP TABLE full2m_eval_report_history_v01 AS
SELECT c.document_id AS historical_document_id,c.title AS title,c.document_url AS document_url,
 c.observed_day AS observed_day,c.metadata_text AS metadata_text
FROM `${corpus_documents}` AS c
JOIN (SELECT DISTINCT m.historical_document_id AS historical_document_id
      FROM `${dataset}.full2m_eval_matches_v01` AS m) AS wanted
 ON wanted.historical_document_id=c.document_id;

WITH fused_top AS (
 SELECT m.query_document_id AS query_document_id,m.historical_document_id AS historical_document_id,
  m.rrf_score AS score
 FROM `${dataset}.full2m_eval_matches_v01` AS m
 WHERE m.rrf_score IS NOT NULL AND m.rrf_score>0
 QUALIFY m.rrf_score=MAX(m.rrf_score) OVER (PARTITION BY m.query_document_id)
), weighted_winners AS (
 SELECT f.query_document_id AS query_document_id,f.historical_document_id AS historical_document_id,
  SAFE_DIVIDE(1.0,COUNT(*) OVER (PARTITION BY f.query_document_id)) AS vote
 FROM fused_top AS f
), date_votes AS (
 SELECT h.observed_day AS observed_day,SUM(w.vote) AS votes,
  COUNT(DISTINCT w.historical_document_id) AS distinct_historical_documents
 FROM weighted_winners AS w
 JOIN `full2m_eval_report_history_v01` AS h ON h.historical_document_id=w.historical_document_id
 GROUP BY h.observed_day
), date_rows AS (
 SELECT v.observed_day AS observed_day,v.votes AS votes,
  v.distinct_historical_documents AS distinct_historical_documents,
  m.anchor_date AS anchor_date,m.day20_date AS day20_date,m.forward_path AS forward_path
 FROM date_votes AS v
 JOIN `${market_paths}` AS m ON m.observed_day=v.observed_day AND m.eligible AND m.price_observations=21
), weighted_path AS (
 SELECT p.horizon_sessions AS horizon_sessions,
  SAFE_DIVIDE(SUM(d.votes*p.price_return),SUM(d.votes)) AS weighted_price_return,
  SUM(d.votes) AS included_vote_weight,COUNT(*) AS historical_date_count
 FROM date_rows AS d CROSS JOIN UNNEST(d.forward_path) AS p
 GROUP BY p.horizon_sessions
)
SELECT * FROM weighted_path ORDER BY horizon_sessions;
