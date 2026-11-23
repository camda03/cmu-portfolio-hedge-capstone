CREATE OR REPLACE TABLE `${dataset}.corpus_manifest` AS WITH block_hashes AS
  (SELECT
     DIV(m.corpus_rank-1, 1000) AS rank_block,
     COUNT(*) AS document_count,
     LOWER(TO_HEX(SHA256(STRING_AGG(CONCAT(CAST(m.corpus_rank AS STRING), '\t', m.document_id, '\n'), ''
                                    ORDER BY m.corpus_rank)))) AS block_sha256
   FROM `${dataset}.corpus_membership` AS m
   GROUP BY rank_block)
SELECT
  SUM(b.document_count) AS document_count,
  LOWER(TO_HEX(SHA256(STRING_AGG(CONCAT(CAST(b.rank_block AS STRING), '\t', b.block_sha256, '\n'), ''
                                 ORDER BY b.rank_block)))) AS corpus_manifest_sha256,
  @contract_sha256 AS build_contract_sha256,
  'Ordered SHA256 of 1000-rank blocks; rank<TAB>document_id<LF> within blocks; block<TAB>hash<LF> across blocks' AS digest_contract
FROM block_hashes AS b;
