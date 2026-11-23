ASSERT
  (SELECT COUNT(*)=COUNT(DISTINCT p.request_id)
   AND COUNT(*)=COUNT(DISTINCT p.metadata_sha256)
   AND COUNT(*)=COUNT(DISTINCT p.first_corpus_rank)
   AND COUNT(*) BETWEEN 1 AND 2000000
   AND SUM(p.document_uses)=2000000
   AND COUNTIF(p.model_input IS NULL
               OR p.model_input=''
               OR BYTE_LENGTH(p.model_input)>6000
               OR p.metadata_sha256 IS DISTINCT
               FROM LOWER(TO_HEX(SHA256(p.model_input)))
               OR p.request_id IS DISTINCT
               FROM LOWER(TO_HEX(SHA256(CONCAT('{"autoTruncate":false,"endpoint":"gemini-embedding-2","outputDimensionality":3072,"representation_version":"gkg-bq-metadata-v03-total-first-complete-labels-6000utf8","task_configuration":"service_default_no_task_parameter"}', '\n', p.metadata_sha256)))))=0
   FROM `${dataset}.filtered_dense_requests_v01` AS p) AS 'Distinct request identities must bind exact complete input text';
