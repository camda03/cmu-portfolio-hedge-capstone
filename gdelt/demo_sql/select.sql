-- Same canonical identities and ordering as the existing intake, but 1..500 is valid.
ASSERT
  (SELECT COUNT(*)=COUNT(DISTINCT document_id)
   FROM `${dataset}.corpus_documents`) AS 'Filtered historical document identities must be unique';

ASSERT
  (SELECT COUNT(*)>0
   FROM `${dataset}.filtered_live_${batch_id}_admitted_documents_v01` AS a
   LEFT JOIN `${dataset}.corpus_documents` AS h ON h.document_id=a.document_id
   WHERE h.document_id IS NULL) AS 'No qualifying fresh documents: keep this receipt; select another explicit closed hour';


CREATE TABLE `${dataset}.filtered_live_${batch_id}_query_documents_v01` CLUSTER BY
  query_request_id,
  query_document_id AS WITH
  fresh AS
  (SELECT
     a.*,
     LOWER(TO_HEX(SHA256(CONCAT(@sample_seed, '\ndocument\n', a.document_id)))) AS document_order
   FROM `${dataset}.filtered_live_${batch_id}_admitted_documents_v01` AS a
   LEFT JOIN `${dataset}.corpus_documents` AS h ON h.document_id=a.document_id
   WHERE h.document_id IS NULL),
  numbered AS
  (SELECT
     f.*,
     ROW_NUMBER() OVER(
                       ORDER BY f.document_order, f.document_id) AS query_rank
   FROM fresh AS f)
SELECT
  n.document_id AS query_document_id,
  n.query_rank,
  LOWER(TO_HEX(SHA256(CONCAT('{"autoTruncate":false,"endpoint":"gemini-embedding-2","outputDimensionality":3072,"representation_version":"gkg-bq-metadata-v03-total-first-complete-labels-6000utf8","task_configuration":"service_default_no_task_parameter"}', '\n', n.metadata_sha256)))) AS query_request_id,
  n.observed_at,
  n.observed_day,
  n.document_url,
  n.title,
  n.metadata_text,
  n.metadata_sha256,
  n.lex_tokens,
  n.quality_flags,
  n.source_record_key,
  n.source_revision_key,
  n.raw_revision_sha256,
  n.sample_admissible_revision_count,
  n.matched_rule_ids,
  n.rules_sha256
FROM numbered AS n
WHERE n.query_rank<=500;

ASSERT
  (SELECT COUNT(*) BETWEEN 1 AND 500
   AND COUNT(DISTINCT query_document_id)=COUNT(*)
   AND MIN(query_rank)=1
   AND MAX(query_rank)=COUNT(*)
   AND COUNTIF(observed_at<@window_start
               OR observed_at>=@window_end
               OR BYTE_LENGTH(metadata_text)>6000
               OR metadata_sha256 IS DISTINCT
               FROM LOWER(TO_HEX(SHA256(metadata_text)))
               OR ARRAY_LENGTH(lex_tokens)=0
               OR ARRAY_LENGTH(matched_rule_ids)=0
               OR rules_sha256!='ed6f55b8ee8450f5faedb009d7f4ff1854d4707c5f0c8ab59755712d5b254b77')=0
   FROM `${dataset}.filtered_live_${batch_id}_query_documents_v01`) AS 'Live query window, representation or admission contract failed';
