CREATE TABLE IF NOT EXISTS `${dataset}.lexical_document_lengths` (document_id STRING,
                                                                    corpus_rank INT64,
                                                                    observed_day DATE,
                                                                    document_length INT64,
                                                                    token_sha256 STRING) CLUSTER BY document_id;


CREATE TABLE IF NOT EXISTS `${dataset}.lexical_postings` (term STRING,document_id STRING,term_frequency INT64) CLUSTER BY
  term,
  document_id;


CREATE TABLE IF NOT EXISTS `${dataset}.lexical_term_stats` (term STRING,document_frequency INT64) CLUSTER BY term;


CREATE TABLE IF NOT EXISTS `${dataset}.lexical_corpus_stats` (corpus_manifest_sha256 STRING,lexical_version STRING,stats_scope STRING,
                                                                document_count INT64,
                                                                total_token_count INT64,
                                                                average_document_length FLOAT64,
                                                                k1 FLOAT64,
                                                                b FLOAT64,
                                                                created_at_utc TIMESTAMP);
