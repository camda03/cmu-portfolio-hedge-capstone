BEGIN TRANSACTION;

ASSERT NOT EXISTS
  (SELECT 1
   FROM `${dataset}.lexical_term_stats`) AS 'Destination is already populated; recover its original build job';

ASSERT EXISTS
  (SELECT 1
   FROM `${dataset}.lexical_postings`) AS 'Build nonempty validated postings before term statistics';


INSERT INTO `${dataset}.lexical_term_stats` (term, document_frequency)
SELECT
  p.term AS term,
  COUNT(*) AS document_frequency
FROM `${dataset}.lexical_postings` AS p
GROUP BY p.term;

ASSERT NOT EXISTS
  (SELECT 1
   FROM `${dataset}.lexical_term_stats`
   WHERE document_frequency<1
     OR document_frequency>@expected_document_count) AS 'Document-frequency statistics are outside the frozen corpus';

ASSERT
  (SELECT COALESCE(SUM(document_frequency), 0)
   FROM `${dataset}.lexical_term_stats`)=
  (SELECT COUNT(*)
   FROM `${dataset}.lexical_postings`) AS 'Term statistics do not cover all postings';


COMMIT TRANSACTION;


SELECT
  COUNT(*) AS term_count,
  SUM(document_frequency) AS posting_count
FROM `${dataset}.lexical_term_stats`;
