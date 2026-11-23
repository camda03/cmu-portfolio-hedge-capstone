ASSERT (TIMESTAMP_DIFF(@window_end, @window_start, SECOND)=3600
        AND @window_end<=@as_of_utc
        AND @partition_start=TIMESTAMP(DATE(@window_start))
        AND @partition_end=TIMESTAMP(DATE_ADD(DATE(TIMESTAMP_SUB(@window_end,INTERVAL 1 MICROSECOND)),INTERVAL 1 DAY))) AS 'Use one frozen closed source hour and its exact UTC partitions';


CREATE TEMP TABLE filtered_live_source_window AS WITH named_source AS
  (SELECT
     CAST(g.`GKGRECORDID` AS STRING) AS `GKGRECORDID`,
     CAST(g.`DATE` AS STRING) AS `DATE`,
     CAST(g.`SourceCollectionIdentifier` AS STRING) AS `SourceCollectionIdentifier`,
     CAST(g.`SourceCommonName` AS STRING) AS `SourceCommonName`,
     CAST(g.`DocumentIdentifier` AS STRING) AS `DocumentIdentifier`,
     CAST(g.`Themes` AS STRING) AS `Themes`,
     CAST(g.`V2Themes` AS STRING) AS `V2Themes`,
     CAST(g.`Persons` AS STRING) AS `Persons`,
     CAST(g.`V2Persons` AS STRING) AS `V2Persons`,
     CAST(g.`Organizations` AS STRING) AS `Organizations`,
     CAST(g.`V2Organizations` AS STRING) AS `V2Organizations`,
     CAST(g.`Locations` AS STRING) AS `Locations`,
     CAST(g.`V2Locations` AS STRING) AS `V2Locations`,
     CAST(g.`TranslationInfo` AS STRING) AS `TranslationInfo`,
     CAST(NULL AS STRING) AS `Title`,
     CAST(g.`Extras` AS STRING) AS `ExtrasXML`
   FROM `gdelt-bq.gdeltv2.gkg_partitioned` AS g
   WHERE g._PARTITIONTIME>=@partition_start
     AND g._PARTITIONTIME<@partition_end
     AND CAST(g.`DATE` AS STRING)>=FORMAT_TIMESTAMP('%Y%m%d%H%M%S', @window_start, 'UTC')
     AND CAST(g.`DATE` AS STRING)<FORMAT_TIMESTAMP('%Y%m%d%H%M%S', @window_end, 'UTC'))
SELECT
  n.`GKGRECORDID` AS `GKGRECORDID`,
  n.`DATE` AS `DATE`,
  n.`SourceCollectionIdentifier` AS `SourceCollectionIdentifier`,
  n.`SourceCommonName` AS `SourceCommonName`,
  n.`DocumentIdentifier` AS `DocumentIdentifier`,
  n.`Themes` AS `Themes`,
  n.`V2Themes` AS `V2Themes`,
  n.`Persons` AS `Persons`,
  n.`V2Persons` AS `V2Persons`,
  n.`Organizations` AS `Organizations`,
  n.`V2Organizations` AS `V2Organizations`,
  n.`Locations` AS `Locations`,
  n.`V2Locations` AS `V2Locations`,
  n.`TranslationInfo` AS `TranslationInfo`,
  n.`Title` AS `Title`,
  n.`ExtrasXML` AS `ExtrasXML`,
  LOWER(TO_HEX(SHA256(TO_JSON_STRING(STRUCT(n.`GKGRECORDID` AS `GKGRECORDID`, n.`DATE` AS `DATE`, n.`SourceCollectionIdentifier` AS `SourceCollectionIdentifier`, n.`SourceCommonName` AS `SourceCommonName`, n.`DocumentIdentifier` AS `DocumentIdentifier`, n.`Themes` AS `Themes`, n.`V2Themes` AS `V2Themes`, n.`Persons` AS `Persons`, n.`V2Persons` AS `V2Persons`, n.`Organizations` AS `Organizations`, n.`V2Organizations` AS `V2Organizations`, n.`Locations` AS `Locations`, n.`V2Locations` AS `V2Locations`, n.`TranslationInfo` AS `TranslationInfo`, n.`Title` AS `Title`, n.`ExtrasXML` AS `ExtrasXML`))))) AS raw_revision_sha256
FROM named_source AS n;


CREATE TABLE `${dataset}.filtered_live_${batch_id}_source_receipt_v01` AS
SELECT
  TIMESTAMP_DIFF(@window_end, @window_start, SECOND) AS source_window_seconds,
  COUNT(*) AS source_rows,
  COUNTIF(r.SourceCollectionIdentifier='1'
          AND COALESCE(TRIM(r.GKGRECORDID), '')!=''
          AND REGEXP_CONTAINS(TRIM(r.DocumentIdentifier), r'(?i)^https?://')) AS qualifying_web_rows,
  COUNT(DISTINCT IF(r.SourceCollectionIdentifier='1'
                    AND COALESCE(TRIM(r.GKGRECORDID), '')!=''
                    AND REGEXP_CONTAINS(TRIM(r.DocumentIdentifier), r'(?i)^https?://'), r.raw_revision_sha256, NULL)) AS distinct_qualifying_raw_revisions,
  TIMESTAMP_DIFF(@as_of_utc, MAX(SAFE.PARSE_TIMESTAMP('%Y%m%d%H%M%S', r.`DATE`, 'UTC')), SECOND) AS latest_source_age_seconds,
  @window_start AS window_start,
  @window_end AS window_end,
  @as_of_utc AS as_of_utc
FROM filtered_live_source_window AS r;


CREATE TABLE `${dataset}.filtered_live_${batch_id}_raw_v01` CLUSTER BY raw_revision_sha256 AS WITH
  distinct_revisions AS
  (SELECT DISTINCT
     r.`GKGRECORDID` AS `GKGRECORDID`,
     r.`DATE` AS `DATE`,
     r.`SourceCollectionIdentifier` AS `SourceCollectionIdentifier`,
     r.`SourceCommonName` AS `SourceCommonName`,
     r.`DocumentIdentifier` AS `DocumentIdentifier`,
     r.`Themes` AS `Themes`,
     r.`V2Themes` AS `V2Themes`,
     r.`Persons` AS `Persons`,
     r.`V2Persons` AS `V2Persons`,
     r.`Organizations` AS `Organizations`,
     r.`V2Organizations` AS `V2Organizations`,
     r.`Locations` AS `Locations`,
     r.`V2Locations` AS `V2Locations`,
     r.`TranslationInfo` AS `TranslationInfo`,
     r.`Title` AS `Title`,
     r.`ExtrasXML` AS `ExtrasXML`,
     r.raw_revision_sha256 AS raw_revision_sha256
   FROM filtered_live_source_window AS r
   WHERE r.SourceCollectionIdentifier='1'
     AND COALESCE(TRIM(r.GKGRECORDID), '')!=''
     AND REGEXP_CONTAINS(TRIM(r.DocumentIdentifier), r'(?i)^https?://')),
  hashed AS
  (SELECT
     s.`GKGRECORDID` AS `GKGRECORDID`,
     s.`DATE` AS `DATE`,
     s.`SourceCollectionIdentifier` AS `SourceCollectionIdentifier`,
     s.`SourceCommonName` AS `SourceCommonName`,
     s.`DocumentIdentifier` AS `DocumentIdentifier`,
     s.`Themes` AS `Themes`,
     s.`V2Themes` AS `V2Themes`,
     s.`Persons` AS `Persons`,
     s.`V2Persons` AS `V2Persons`,
     s.`Organizations` AS `Organizations`,
     s.`V2Organizations` AS `V2Organizations`,
     s.`Locations` AS `Locations`,
     s.`V2Locations` AS `V2Locations`,
     s.`TranslationInfo` AS `TranslationInfo`,
     s.`Title` AS `Title`,
     s.`ExtrasXML` AS `ExtrasXML`,
     s.raw_revision_sha256 AS raw_revision_sha256,
     LOWER(TO_HEX(SHA256(CONCAT(@sample_seed, '\n', s.raw_revision_sha256)))) AS candidate_order
   FROM distinct_revisions AS s),
  selected AS
  (SELECT
     s.`GKGRECORDID` AS `GKGRECORDID`,
     s.`DATE` AS `DATE`,
     s.`SourceCollectionIdentifier` AS `SourceCollectionIdentifier`,
     s.`SourceCommonName` AS `SourceCommonName`,
     s.`DocumentIdentifier` AS `DocumentIdentifier`,
     s.`Themes` AS `Themes`,
     s.`V2Themes` AS `V2Themes`,
     s.`Persons` AS `Persons`,
     s.`V2Persons` AS `V2Persons`,
     s.`Organizations` AS `Organizations`,
     s.`V2Organizations` AS `V2Organizations`,
     s.`Locations` AS `Locations`,
     s.`V2Locations` AS `V2Locations`,
     s.`TranslationInfo` AS `TranslationInfo`,
     s.`Title` AS `Title`,
     s.`ExtrasXML` AS `ExtrasXML`,
     s.raw_revision_sha256 AS raw_revision_sha256,
     s.candidate_order AS candidate_order
   FROM hashed AS s QUALIFY ROW_NUMBER() OVER(
                                              ORDER BY s.candidate_order, s.raw_revision_sha256)<=@raw_limit)
SELECT
  s.`GKGRECORDID` AS `GKGRECORDID`,
  s.`DATE` AS `DATE`,
  s.`SourceCollectionIdentifier` AS `SourceCollectionIdentifier`,
  s.`SourceCommonName` AS `SourceCommonName`,
  s.`DocumentIdentifier` AS `DocumentIdentifier`,
  s.`Themes` AS `Themes`,
  s.`V2Themes` AS `V2Themes`,
  s.`Persons` AS `Persons`,
  s.`V2Persons` AS `V2Persons`,
  s.`Organizations` AS `Organizations`,
  s.`V2Organizations` AS `V2Organizations`,
  s.`Locations` AS `Locations`,
  s.`V2Locations` AS `V2Locations`,
  s.`TranslationInfo` AS `TranslationInfo`,
  s.`Title` AS `Title`,
  s.`ExtrasXML` AS `ExtrasXML`,
  s.raw_revision_sha256 AS raw_revision_sha256,
  FORMAT_TIMESTAMP('%Y-%m', SAFE.PARSE_TIMESTAMP('%Y%m%d%H%M%S', s.`DATE`, 'UTC'), 'UTC') AS batch_month,
  FORMAT_TIMESTAMP('%Y-%m-%d', SAFE.PARSE_TIMESTAMP('%Y%m%d%H%M%S', s.`DATE`, 'UTC'), 'UTC') AS candidate_day,
  s.candidate_order AS candidate_order,
  CAST(NULL AS INT64) AS source_rows_for_raw_url_day,
  CAST(NULL AS INT64) AS source_record_ids_for_raw_url_day
FROM selected AS s;

ASSERT
  (SELECT COUNT(*) BETWEEN 500 AND @raw_limit
   AND COUNT(DISTINCT raw_revision_sha256)=COUNT(*)
   FROM `${dataset}.filtered_live_${batch_id}_raw_v01`) AS 'The bounded source sample is too small or contains conflicting raw revisions';
