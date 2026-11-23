BEGIN TRANSACTION;

ASSERT NOT EXISTS
  (SELECT 1
   FROM `${dataset}.lexical_document_lengths`) AS 'Destination is already populated; recover its original build job';

ASSERT @expected_document_count BETWEEN 1 AND 2000000 AS 'Frozen corpus count is outside the reviewed bound';


INSERT INTO `${dataset}.lexical_document_lengths` (document_id, corpus_rank, observed_day, document_length, token_sha256)
SELECT
  d.document_id AS document_id,
  d.corpus_rank AS corpus_rank,
  d.observed_day AS observed_day,
  ARRAY_LENGTH(d.lex_tokens) AS document_length,
  LOWER(TO_HEX(SHA256(TO_JSON_STRING(d.lex_tokens)))) AS token_sha256
FROM `${dataset}.corpus_documents` AS d;

ASSERT
  (SELECT COUNT(*)=@expected_document_count
   AND COUNT(DISTINCT document_id)=@expected_document_count
   AND COUNT(DISTINCT corpus_rank)=@expected_document_count
   AND MIN(corpus_rank)=1
   AND MAX(corpus_rank)=@expected_document_count
   AND COUNTIF(document_id IS NULL
               OR document_id=''
               OR observed_day IS NULL
               OR document_length IS NULL
               OR document_length<=0)=0
   FROM `${dataset}.lexical_document_lengths`) AS 'Frozen documents are missing, repeated or contain empty lexical inputs';


COMMIT TRANSACTION;


SELECT
  COUNT(*) AS document_count,
  SUM(document_length) AS total_token_count
FROM `${dataset}.lexical_document_lengths`;
