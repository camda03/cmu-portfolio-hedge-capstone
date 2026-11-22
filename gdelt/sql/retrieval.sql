-- One cohort, two rankings. The live path requests 22 vector neighbors, removes identical inputs,

-- and keeps the best 20 in each channel. The production table names are retained.

CREATE TABLE `${dataset}.full2m_eval_queries_v01` CLUSTER BY query_key,query_document_id AS
WITH normalized AS (
 SELECT q.query_document_id AS query_document_id,q.query_request_id AS query_request_id,
 TO_JSON_STRING(ARRAY(SELECT DISTINCT token FROM UNNEST(q.lex_tokens) AS token ORDER BY token)) AS lexical_terms_json
 FROM `${query_documents}` AS q
)
SELECT q.query_document_id AS query_document_id,q.query_request_id AS query_request_id,q.lexical_terms_json AS lexical_terms_json,
 DENSE_RANK() OVER(ORDER BY q.lexical_terms_json) AS query_key FROM normalized AS q;
CREATE TABLE `${dataset}.full2m_eval_query_terms_v01` CLUSTER BY term_key,query_key AS
SELECT DISTINCT q.query_key AS query_key,t.term_key AS term_key
FROM `${dataset}.full2m_eval_queries_v01` AS q CROSS JOIN UNNEST(JSON_VALUE_ARRAY(q.lexical_terms_json)) AS token
JOIN `${bm25_terms}` AS t ON t.term=token;

ASSERT (@request_k=IF(@exclude_identical_inputs,22,20))
 AS 'Retained baseline searches 20 request neighbors; filtered mode searches 22 before exclusions';
CREATE TABLE `${dataset}.full2m_eval_ivf40_request_matches_v01` CLUSTER BY query_request_id AS
SELECT query.query_request_id AS query_request_id,base.request_id AS matched_request_id,distance AS cosine_distance
FROM VECTOR_SEARCH(TABLE `${dense_vectors}`,'embedding',
 (SELECT q.query_request_id AS query_request_id,q.embedding AS embedding
 FROM `${query_vectors}` AS q
 WHERE q.query_request_id IN (SELECT query_request_id FROM `${query_documents}`)),
 'embedding',top_k=>@request_k,distance_type=>'COSINE',options=>'{"fraction_lists_to_search":0.4}');
CREATE TABLE `${dataset}.full2m_eval_ivf40_matches_v01` CLUSTER BY query_document_id AS
WITH expanded AS (
 SELECT q.query_document_id AS query_document_id,q.query_request_id AS query_request_id,
 h.document_id AS historical_document_id,n.matched_request_id AS matched_request_id,n.cosine_distance AS cosine_distance,
 ROW_NUMBER() OVER(PARTITION BY q.query_document_id ORDER BY n.cosine_distance,h.document_id) AS cosine_rank
 FROM `${dataset}.full2m_eval_ivf40_request_matches_v01` AS n JOIN `${query_documents}` AS q ON q.query_request_id=n.query_request_id
 JOIN `${dense_documents}` AS h ON h.request_id=n.matched_request_id
 WHERE h.document_id!=q.query_document_id
 AND (NOT @exclude_identical_inputs OR n.matched_request_id!=q.query_request_id)
)
SELECT e.query_document_id AS query_document_id,e.query_request_id AS query_request_id,
 e.historical_document_id AS historical_document_id,e.matched_request_id AS matched_request_id,
 e.cosine_distance AS cosine_distance,1.0-e.cosine_distance AS cosine_similarity,e.cosine_rank AS cosine_rank
FROM expanded AS e WHERE e.cosine_rank<=20;

-- Only the filtered live BM25 branch is needed for this selection.

ASSERT @exclude_identical_inputs AS 'Filtered GDELT retrieval requires identical-input exclusion';

CREATE TABLE `${dataset}.full2m_eval_bm25_matches_v01` CLUSTER BY query_document_id AS
WITH batch_terms AS (SELECT DISTINCT q.term_key AS term_key FROM `${dataset}.full2m_eval_query_terms_v01` AS q), candidate_scores AS (
 SELECT q.query_key AS query_key,p.historical_key AS historical_key,SUM(p.term_score_exact) AS bm25_score_exact
 FROM `${bm25_weights}` AS p JOIN `${dataset}.full2m_eval_query_terms_v01` AS q ON q.term_key=p.term_key
 WHERE p.term_key IN (SELECT b.term_key FROM batch_terms AS b) GROUP BY q.query_key,p.historical_key
), eligible AS (
 SELECT q.query_document_id AS query_document_id,q.query_request_id AS query_request_id,h.historical_document_id AS historical_document_id,
 m.request_id AS matched_request_id,s.bm25_score_exact AS bm25_score_exact
 FROM candidate_scores AS s JOIN `${dataset}.full2m_eval_queries_v01` AS q ON q.query_key=s.query_key
 JOIN `${bm25_history}` AS h ON h.historical_key=s.historical_key
 JOIN `${dense_documents}` AS m ON m.document_id=h.historical_document_id
 WHERE h.historical_document_id!=q.query_document_id AND m.request_id!=q.query_request_id
), best AS (
 SELECT e.query_document_id AS query_document_id,
 ARRAY_AGG(STRUCT(e.query_request_id AS query_request_id,e.historical_document_id AS historical_document_id,
 e.matched_request_id AS matched_request_id,e.bm25_score_exact AS bm25_score_exact)
 ORDER BY e.bm25_score_exact DESC,e.historical_document_id LIMIT 20) AS matches
 FROM eligible AS e GROUP BY e.query_document_id
)
SELECT b.query_document_id AS query_document_id,x.query_request_id AS query_request_id,
 x.historical_document_id AS historical_document_id,x.matched_request_id AS matched_request_id,
 x.bm25_score_exact AS bm25_score_exact,CAST(x.bm25_score_exact AS FLOAT64) AS bm25_score,rank_offset+1 AS bm25_rank
FROM best AS b CROSS JOIN UNNEST(b.matches) AS x WITH OFFSET AS rank_offset;

CREATE TABLE `${dataset}.full2m_eval_matches_v01` CLUSTER BY query_document_id AS
WITH combined AS (
 SELECT COALESCE(c.query_document_id,b.query_document_id) AS query_document_id,
 COALESCE(c.historical_document_id,b.historical_document_id) AS historical_document_id,
 c.cosine_distance AS cosine_distance,c.cosine_similarity AS cosine_similarity,c.cosine_rank AS cosine_rank,
 b.bm25_score_exact AS bm25_score_exact,b.bm25_score AS bm25_score,b.bm25_rank AS bm25_rank,
 IF(c.cosine_rank IS NULL,0.0,1.0/(60.0+c.cosine_rank))+
 IF(b.bm25_rank IS NULL,0.0,1.0/(60.0+b.bm25_rank)) AS rrf_score
 FROM `${dataset}.full2m_eval_ivf40_matches_v01` AS c FULL OUTER JOIN `${dataset}.full2m_eval_bm25_matches_v01` AS b
 ON b.query_document_id=c.query_document_id AND b.historical_document_id=c.historical_document_id
)
SELECT c.query_document_id AS query_document_id,c.historical_document_id AS historical_document_id,
 c.cosine_distance AS cosine_distance,c.cosine_similarity AS cosine_similarity,c.cosine_rank AS cosine_rank,
 c.bm25_score_exact AS bm25_score_exact,c.bm25_score AS bm25_score,c.bm25_rank AS bm25_rank,c.rrf_score AS rrf_score,
 ROW_NUMBER() OVER(PARTITION BY c.query_document_id ORDER BY c.rrf_score DESC,c.historical_document_id) AS fused_rank
FROM combined AS c;
