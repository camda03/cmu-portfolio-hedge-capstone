-- Reuse verified historical vectors for exact input identities. Embed and publish only new inputs.

-- Reuse only exact request identities from the verified filtered build.
CREATE TABLE `${dataset}.demo_cache_v01` CLUSTER BY request_id AS
SELECT v.request_id,v.embedding FROM `${dense_vectors}` AS v
JOIN `${intake_requests}` AS r ON r.request_id=v.request_id;
ASSERT (SELECT COUNT(*)=COUNT(DISTINCT request_id) FROM `${dataset}.demo_cache_v01`)
 AS 'Cache identities must be unique';
CREATE TABLE `${query_requests}` CLUSTER BY request_id AS
SELECT r.*,c.request_id IS NOT NULL AS cached
FROM `${intake_requests}` AS r LEFT JOIN `${dataset}.demo_cache_v01` AS c USING(request_id);
ASSERT (SELECT COUNT(*) BETWEEN 1 AND 500 AND COUNT(*)=COUNT(DISTINCT request_id)
 AND SUM(document_uses)=@expected_query_documents AND COUNTIF(input_variant_count IS DISTINCT FROM 1
 OR metadata_sha256 IS DISTINCT FROM LOWER(TO_HEX(SHA256(model_input)))
 OR model_input IS NULL OR BYTE_LENGTH(model_input) NOT BETWEEN 1 AND 6000)=0
 FROM `${query_requests}`) AS 'Live requests must bind to the admitted query cohort';

CREATE TABLE `${dataset}.filtered_live_${batch_id}_request_candidates_v01` CLUSTER BY request_id AS
SELECT q.query_request_id AS request_id,q.metadata_sha256,MIN(q.metadata_text) AS model_input,
 COUNT(*) AS document_uses,COUNT(DISTINCT q.metadata_text) AS input_variant_count,
 '5956ed371c5c56eb1a01402236ca70b4ab383b078b7df7bb0e51bffe37f828bc' AS rules_sha256
FROM `${dataset}.filtered_live_${batch_id}_query_documents_v01` AS q
GROUP BY q.query_request_id,q.metadata_sha256;
ASSERT (SELECT COUNT(*) BETWEEN 1 AND 500 AND COUNT(DISTINCT request_id)=COUNT(*)
 AND SUM(document_uses)=(SELECT COUNT(*) FROM `${dataset}.filtered_live_${batch_id}_query_documents_v01`)
 AND COUNTIF(input_variant_count!=1 OR r.model_input IS NULL OR LENGTH(TRIM(r.model_input))=0
 OR BYTE_LENGTH(r.model_input)>6000
 OR r.metadata_sha256 IS DISTINCT FROM LOWER(TO_HEX(SHA256(r.model_input)))
 OR r.request_id IS DISTINCT FROM LOWER(TO_HEX(SHA256(CONCAT('{"autoTruncate":false,"endpoint":"gemini-embedding-2","outputDimensionality":3072,"representation_version":"gkg-bq-metadata-v03-total-first-complete-labels-6000utf8","task_configuration":"service_default_no_task_parameter"}','\n',r.metadata_sha256)))))=0
 FROM `${dataset}.filtered_live_${batch_id}_request_candidates_v01` AS r) AS 'Requests must bind exactly to the selected documents';

-- The runner records a durable job ID before submission; no automatic paid-job retry.
ASSERT @new_inputs BETWEEN 0 AND 500
 AND (SELECT COUNTIF(NOT cached) FROM `${query_requests}`)=@new_inputs
 AS 'New live embedding count differs from the bounded batch';
CREATE TABLE `${dataset}.demo_raw_embeddings_v01` AS
SELECT request_id,
 AI.EMBED(model_input,endpoint=>'gemini-embedding-2',
 model_params=>JSON '{"outputDimensionality":3072,"autoTruncate":false}') AS outcome
FROM `${query_requests}` WHERE NOT cached;

ASSERT (SELECT COUNT(*)=COUNT(DISTINCT request_id)
 AND COUNT(*)=(SELECT COUNTIF(NOT cached) FROM `${query_requests}`)
 AND COUNTIF(outcome.status IS NULL OR outcome.status!='')=0
 FROM `${dataset}.demo_raw_embeddings_v01`) AS 'Missing, duplicated, or unsuccessful live embeddings';
ASSERT NOT EXISTS (
 SELECT 1 FROM `${dataset}.demo_raw_embeddings_v01` AS e
 LEFT JOIN `${query_requests}` AS r USING(request_id)
 WHERE r.request_id IS NULL OR r.cached) AS 'Unexpected live embedding outputs';
CREATE TABLE `${query_vectors}` CLUSTER BY query_request_id AS
SELECT r.request_id AS query_request_id,r.document_uses,TRUE AS cached,c.embedding
FROM `${query_requests}` AS r JOIN `${dataset}.demo_cache_v01` AS c USING(request_id) WHERE r.cached
UNION ALL BY NAME
SELECT r.request_id AS query_request_id,r.document_uses,FALSE AS cached,e.outcome.result AS embedding
FROM `${query_requests}` AS r JOIN `${dataset}.demo_raw_embeddings_v01` AS e USING(request_id) WHERE NOT r.cached;
ASSERT (SELECT COUNT(*)=COUNT(DISTINCT query_request_id)
 AND COUNT(*)=(SELECT COUNT(*) FROM `${query_requests}`) AND SUM(document_uses)=@expected_query_documents
 AND COUNTIF(ARRAY_LENGTH(embedding) IS DISTINCT FROM 3072
 OR EXISTS(SELECT 1 FROM UNNEST(embedding) AS x WHERE x IS NULL OR IS_NAN(x) OR IS_INF(x))
 OR COALESCE((SELECT MAX(ABS(x)) FROM UNNEST(embedding) AS x),0)<=0)=0
 FROM `${query_vectors}`) AS 'Live vectors must be present, finite, nonzero and 3072-dimensional';
