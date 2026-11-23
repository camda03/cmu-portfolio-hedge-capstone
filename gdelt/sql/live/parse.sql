CREATE TABLE `${dataset}.filtered_live_${batch_id}_parsed_v01` CLUSTER BY raw_revision_sha256 AS
SELECT
  r.raw_revision_sha256 AS raw_revision_sha256,
  r.batch_month AS batch_month,
  r.candidate_day AS candidate_day,
  r.candidate_order AS candidate_order,
  r.source_rows_for_raw_url_day AS source_rows_for_raw_url_day,
  r.source_record_ids_for_raw_url_day AS source_record_ids_for_raw_url_day,
  `${dataset}.gkg_parse_source_v01`(TO_JSON_STRING(STRUCT(r.`GKGRECORDID` AS `GKGRECORDID`, r.`DATE` AS `DATE`, r.`SourceCollectionIdentifier` AS `SourceCollectionIdentifier`, r.`SourceCommonName` AS `SourceCommonName`, r.`DocumentIdentifier` AS `DocumentIdentifier`, r.`Themes` AS `Themes`, r.`V2Themes` AS `V2Themes`, r.`Persons` AS `Persons`, r.`V2Persons` AS `V2Persons`, r.`Organizations` AS `Organizations`, r.`V2Organizations` AS `V2Organizations`, r.`Locations` AS `Locations`, r.`V2Locations` AS `V2Locations`, r.`TranslationInfo` AS `TranslationInfo`, r.`Title` AS `Title`, r.`ExtrasXML` AS `ExtrasXML`))) AS p
FROM `${dataset}.filtered_live_${batch_id}_raw_v01` AS r ;
