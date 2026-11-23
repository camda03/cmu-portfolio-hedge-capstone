BEGIN TRANSACTION;

ASSERT NOT EXISTS
  (SELECT 1
   FROM `${dataset}.lexical_corpus_stats`) AS 'Destination is already populated; recover its original build job';

ASSERT
  (SELECT COUNT(*)=1
   AND COUNTIF(document_count IS DISTINCT
               FROM @expected_document_count
               OR corpus_manifest_sha256 IS NULL
               OR NOT REGEXP_CONTAINS(corpus_manifest_sha256, r'^[0-9a-f]{64}$'))=0
   FROM `${dataset}.corpus_manifest`) AS 'The server-side frozen membership manifest is missing or differs';

ASSERT
  (SELECT COUNT(DISTINCT document_id)=@expected_document_count
   FROM `${dataset}.lexical_postings`) AS 'Posting coverage is incomplete';

ASSERT
  (SELECT COUNT(*)>0
   FROM `${dataset}.lexical_term_stats`) AS 'Build term statistics before corpus publication';


INSERT INTO `${dataset}.lexical_corpus_stats` (corpus_manifest_sha256, lexical_version, stats_scope, document_count, total_token_count, average_document_length, k1, b, created_at_utc)
SELECT
  m.corpus_manifest_sha256 AS corpus_manifest_sha256,
  'gkg-bigquery-bm25-positive-idf-v01-r01' AS lexical_version,
  'FULL_FROZEN_CORPUS' AS stats_scope,
  COUNT(*) AS document_count,
  SUM(l.document_length) AS total_token_count,
  AVG(CAST(l.document_length AS FLOAT64)) AS average_document_length,
  1.2 AS k1,
  0.75 AS b,
  CURRENT_TIMESTAMP() AS created_at_utc
FROM `${dataset}.lexical_document_lengths` AS l
CROSS JOIN `${dataset}.corpus_manifest` AS m
GROUP BY m.corpus_manifest_sha256;

ASSERT
  (SELECT COUNT(*)=1
   AND COUNTIF(document_count!=@expected_document_count
               OR average_document_length IS NULL
               OR average_document_length<=0)=0
   FROM `${dataset}.lexical_corpus_stats`) AS 'Corpus statistics do not match the frozen complete corpus';


COMMIT TRANSACTION;


SELECT
  s.document_count AS document_count,
  s.total_token_count AS total_token_count,
  s.average_document_length AS average_document_length
FROM `${dataset}.lexical_corpus_stats` AS s
LIMIT 1;
