SELECT
  document_count,
  corpus_manifest_sha256,
  build_contract_sha256
FROM `${dataset}.corpus_manifest`
LIMIT 2;
