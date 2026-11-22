-- The historical corpus is selected by day. These serving tables supply the cosine and BM25 searches.

CREATE OR REPLACE TABLE `${dataset}.corpus_documents`
PARTITION BY observed_day CLUSTER BY corpus_rank,document_id AS
SELECT m.document_id AS document_id,m.corpus_rank AS corpus_rank,m.observed_day AS observed_day,
 m.in_checkpoint_100k AS in_checkpoint_100k,m.in_checkpoint_1m AS in_checkpoint_1m,
 n.observed_at AS observed_at,n.source_record_key AS source_record_key,n.source_revision_key AS source_revision_key,
 n.raw_revision_sha256 AS raw_revision_sha256,n.document_url AS document_url,n.title AS title,
 n.metadata_text AS metadata_text,n.metadata_sha256 AS metadata_sha256,n.lex_tokens AS lex_tokens,
 n.quality_flags AS quality_flags,p.anchor_date AS market_anchor_date,p.day20_date AS market_day20_date
FROM `${dataset}.corpus_membership` AS m JOIN `${dataset}.normalized_observations` AS n
 ON n.raw_revision_sha256=m.raw_revision_sha256 AND n.document_id=m.document_id
JOIN `${dataset}.market_paths` AS p ON p.observed_day=m.observed_day AND p.eligible
WHERE n.admissible;

CREATE VECTOR INDEX filtered_dense_vector_index_v01 ON `${dataset}.filtered_dense_vectors_v01`(embedding)
STORING(request_id,full_input_rank,first_corpus_rank)
OPTIONS(index_type='IVF',distance_type='COSINE',ivf_options='{"num_lists":2048}');

CREATE TABLE `${dataset}.filtered_stable_bm25_history_v01` CLUSTER BY historical_key AS
SELECT ROW_NUMBER() OVER(ORDER BY x.document_id) AS historical_key,x.document_id AS historical_document_id FROM `${dataset}.corpus_documents` AS x;
CREATE TABLE `${dataset}.filtered_stable_bm25_terms_v01` CLUSTER BY term AS
SELECT ROW_NUMBER() OVER(ORDER BY x.term) AS term_key,x.term AS term,x.document_frequency AS document_frequency FROM `${dataset}.lexical_term_stats` AS x;
CREATE TABLE `${dataset}.filtered_stable_bm25_weights_v01` CLUSTER BY term_key,historical_key AS
SELECT t.term_key AS term_key,h.historical_key AS historical_key,
 CAST(LN(1.0+(c.document_count-t.document_frequency+0.5)/(t.document_frequency+0.5))
 *(p.term_frequency*(c.k1+1.0)) /
 (p.term_frequency+c.k1*(1.0-c.b+c.b*l.document_length/c.average_document_length)) AS BIGNUMERIC) AS term_score_exact
FROM `${dataset}.lexical_postings` AS p JOIN `${dataset}.filtered_stable_bm25_terms_v01` AS t ON t.term=p.term JOIN `${dataset}.filtered_stable_bm25_history_v01` AS h ON h.historical_document_id=p.document_id
JOIN `${dataset}.lexical_document_lengths` AS l ON l.document_id=p.document_id CROSS JOIN `${dataset}.lexical_corpus_stats` AS c;
