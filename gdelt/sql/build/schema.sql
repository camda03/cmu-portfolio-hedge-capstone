CREATE TABLE IF NOT EXISTS `${dataset}.raw_candidates` (`GKGRECORDID` STRING, `DATE` STRING, `SourceCollectionIdentifier` STRING, `SourceCommonName` STRING, `DocumentIdentifier` STRING, `Themes` STRING, `V2Themes` STRING, `Persons` STRING, `V2Persons` STRING, `Organizations` STRING, `V2Organizations` STRING, `Locations` STRING, `V2Locations` STRING, `TranslationInfo` STRING, `Title` STRING, `ExtrasXML` STRING, candidate_day STRING, candidate_order STRING,
                                                          source_rows_for_raw_url_day INT64,
                                                          source_record_ids_for_raw_url_day INT64,
                                                          batch_month STRING, source_day DATE, raw_revision_sha256 STRING) PARTITION BY source_day CLUSTER BY
  SourceCollectionIdentifier,
  DocumentIdentifier,
  GKGRECORDID;


CREATE TABLE IF NOT EXISTS `${dataset}.daily_quotas` (observed_day DATE,
                                                        target_documents INT64,
                                                        stratum_order STRING,
                                                        checkpoint_100k_target INT64,
                                                        checkpoint_1m_target INT64) CLUSTER BY observed_day;


CREATE TABLE IF NOT EXISTS `${dataset}.market_closes` (session_date DATE,
                                                         close_price FLOAT64,
                                                         available_at TIMESTAMP) CLUSTER BY session_date;


CREATE TABLE IF NOT EXISTS `${dataset}.build_contract` (run_id STRING, contract_sha256 STRING, contract_json STRING, created_at TIMESTAMP);
