BEGIN TRANSACTION;

ASSERT NOT EXISTS
  (SELECT 1
   FROM `${dataset}.lexical_postings`) AS 'Destination is already populated; recover its original build job';

ASSERT
  (SELECT COUNT(*)=@expected_document_count
   FROM `${dataset}.lexical_document_lengths`) AS 'Build document lengths before postings';


INSERT INTO `${dataset}.lexical_postings` (term, document_id, term_frequency)
SELECT
  token AS term,
  d.document_id AS document_id,
  COUNT(*) AS term_frequency
FROM `${dataset}.corpus_documents` AS d
JOIN `${dataset}.lexical_document_lengths` AS l ON l.document_id=d.document_id
AND l.token_sha256=LOWER(TO_HEX(SHA256(TO_JSON_STRING(d.lex_tokens))))
CROSS JOIN UNNEST(d.lex_tokens) AS token
GROUP BY
  token,
  d.document_id;

ASSERT NOT EXISTS
  (SELECT 1
   FROM `${dataset}.lexical_postings`
   WHERE term IS NULL
     OR term=''
     OR term_frequency<=0) AS 'Postings contain invalid atomic terms';

ASSERT
  (SELECT COUNT(DISTINCT document_id)=@expected_document_count
   FROM `${dataset}.lexical_postings`) AS 'Frozen input changed or postings have missing documents';

ASSERT
  (SELECT COALESCE(SUM(term_frequency), 0)
   FROM `${dataset}.lexical_postings`)=
  (SELECT SUM(document_length)
   FROM `${dataset}.lexical_document_lengths`) AS 'Posting frequencies differ from the frozen token manifest';


COMMIT TRANSACTION;


SELECT
  COUNT(*) AS posting_count,
  SUM(term_frequency) AS total_token_count
FROM `${dataset}.lexical_postings`;
