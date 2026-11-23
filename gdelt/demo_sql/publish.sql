ASSERT
  (SELECT COUNT(*)=COUNT(DISTINCT request_id)
   AND COUNT(*)=
     (SELECT COUNTIF(NOT cached)
      FROM `${query_requests}`)
   AND COUNTIF(outcome.status IS NULL
               OR outcome.status!='')=0
   FROM `${dataset}.demo_raw_embeddings_v01`) AS 'Missing, duplicated, or unsuccessful live embeddings';

ASSERT NOT EXISTS
  (SELECT 1
   FROM `${dataset}.demo_raw_embeddings_v01` AS e
   LEFT JOIN `${query_requests}` AS r USING(request_id)
   WHERE r.request_id IS NULL
     OR r.cached) AS 'Unexpected live embedding outputs';


CREATE TABLE `${query_vectors}` CLUSTER BY query_request_id AS
SELECT
  r.request_id AS query_request_id,
  r.document_uses,
  TRUE AS
  cached,
  c.embedding
FROM `${query_requests}` AS r
JOIN `${dataset}.demo_cache_v01` AS c USING(request_id)
WHERE r.cached
UNION ALL BY NAME
SELECT
  r.request_id AS query_request_id,
  r.document_uses,
  FALSE AS
  cached,
  e.outcome.result AS embedding
FROM `${query_requests}` AS r
JOIN `${dataset}.demo_raw_embeddings_v01` AS e USING(request_id)
WHERE NOT r.cached;

ASSERT
  (SELECT COUNT(*)=COUNT(DISTINCT query_request_id)
   AND COUNT(*)=
     (SELECT COUNT(*)
      FROM `${query_requests}`)
   AND SUM(document_uses)=@expected_query_documents
   AND COUNTIF(ARRAY_LENGTH(embedding) IS DISTINCT
               FROM 3072
               OR EXISTS
                 (SELECT 1
                  FROM UNNEST(embedding) AS x
                  WHERE x IS NULL
                    OR IS_NAN(x)
                    OR IS_INF(x))
               OR COALESCE(
                             (SELECT MAX(ABS(x))
                              FROM UNNEST(embedding) AS x),0)<=0)=0
   FROM `${query_vectors}`) AS 'Live vectors must be present, finite, nonzero and 3072-dimensional';
