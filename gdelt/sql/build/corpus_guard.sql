ASSERT
  (SELECT documents=2000000
   AND distinct_documents=2000000
   AND distinct_ranks=2000000
   AND first_rank=1
   AND last_rank=2000000
   AND checkpoint_100k_documents=100000
   AND checkpoint_1m_documents=1000000
   AND invalid_documents=0
   AND represented_days=3626
   FROM
     (SELECT COUNT(*) AS documents, COUNT(DISTINCT c.document_id) AS distinct_documents, COUNT(DISTINCT c.corpus_rank) AS distinct_ranks, MIN(c.corpus_rank) AS first_rank, MAX(c.corpus_rank) AS last_rank, COUNTIF(c.in_checkpoint_100k) AS checkpoint_100k_documents, COUNTIF(c.in_checkpoint_1m) AS checkpoint_1m_documents, COUNTIF(ARRAY_LENGTH(c.lex_tokens)=0
                                                                                                                                                                                                                                                                                                                                         OR c.metadata_text IS NULL
                                                                                                                                                                                                                                                                                                                                         OR BYTE_LENGTH(c.metadata_text)>6000) AS invalid_documents, COUNT(DISTINCT c.metadata_sha256) AS distinct_metadata_inputs, COUNT(DISTINCT c.observed_day) AS represented_days
      FROM `${dataset}.corpus_documents` AS c
      LIMIT 1)) AS 'Frozen corpus is not exactly the approved two million';

ASSERT
  (SELECT documents=2000000
   AND invalid_filter_documents=0
   AND invalid_metadata_hashes=0
   FROM
     (SELECT COUNT(*) AS documents, COUNTIF(f.document_id IS NULL
                                            OR NOT f.expanded_rule_selected
                                            OR f.rules_sha256!='ed6f55b8ee8450f5faedb009d7f4ff1854d4707c5f0c8ab59755712d5b254b77') AS invalid_filter_documents, COUNT(DISTINCT c.document_id) AS distinct_documents, COUNT(DISTINCT c.raw_revision_sha256) AS distinct_revisions, COUNTIF(c.metadata_sha256!=LOWER(TO_HEX(SHA256(c.metadata_text)))) AS invalid_metadata_hashes
      FROM `${dataset}.corpus_documents` AS c
      LEFT JOIN `${dataset}.candidate_filter_decisions` AS f ON f.document_id=c.document_id
      AND f.raw_revision_sha256=c.raw_revision_sha256)) AS 'Expanded admission or metadata fingerprint differs';

ASSERT
  (SELECT planned_days=3626
   AND final_quota_errors=0
   AND checkpoint_100k_errors=0
   AND checkpoint_1m_errors=0
   FROM
     (SELECT COUNT(*) AS planned_days, COUNTIF(q.target_documents IS DISTINCT
                                               FROM c.documents) AS final_quota_errors, COUNTIF(q.checkpoint_100k_target IS DISTINCT
                                                                                                FROM c.checkpoint_100k_documents) AS checkpoint_100k_errors, COUNTIF(q.checkpoint_1m_target IS DISTINCT
                                                                                                                                                                     FROM c.checkpoint_1m_documents) AS checkpoint_1m_errors
      FROM `${dataset}.daily_quotas` AS q
      LEFT JOIN
        (SELECT m.observed_day AS observed_day, COUNT(*) AS documents, COUNTIF(m.in_checkpoint_100k) AS checkpoint_100k_documents, COUNTIF(m.in_checkpoint_1m) AS checkpoint_1m_documents
         FROM `${dataset}.corpus_membership` AS m
         GROUP BY m.observed_day) AS c ON c.observed_day=q.observed_day
      LIMIT 1)) AS 'Frozen daily or checkpoint quotas differ';

ASSERT
  (SELECT COUNT(*)=1
   AND COUNTIF(document_count IS DISTINCT
               FROM 2000000
               OR build_contract_sha256 IS DISTINCT
               FROM @contract_sha256
               OR corpus_manifest_sha256 IS NULL
               OR NOT REGEXP_CONTAINS(corpus_manifest_sha256, r'^[0-9a-f]{64}$'))=0
   FROM `${dataset}.corpus_manifest`) AS 'Frozen corpus manifest differs';
