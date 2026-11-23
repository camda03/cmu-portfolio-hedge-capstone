-- The runner records a durable job ID before submission; no automatic paid-job retry.
ASSERT @new_inputs BETWEEN 0 AND 500
AND
  (SELECT COUNTIF(NOT cached)
   FROM `${query_requests}`)=@new_inputs AS 'New live embedding count differs from the bounded batch';


CREATE TABLE `${dataset}.demo_raw_embeddings_v01` AS
SELECT
  request_id,
  AI.EMBED(model_input, endpoint=>'gemini-embedding-2', model_params=>JSON '{"outputDimensionality":3072,"autoTruncate":false}') AS outcome
FROM `${query_requests}`
WHERE NOT cached;
