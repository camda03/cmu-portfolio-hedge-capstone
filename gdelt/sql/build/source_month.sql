MERGE `${dataset}.raw_candidates` AS t USING
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
     CAST(NULL AS STRING) AS `Title`,
     s.`ExtrasXML` AS `ExtrasXML`,
     s.candidate_day AS candidate_day,
     s.candidate_order AS candidate_order,
     s.source_rows_for_raw_url_day AS source_rows_for_raw_url_day,
     s.source_record_ids_for_raw_url_day AS source_record_ids_for_raw_url_day,
     @batch_month AS batch_month,
     CAST(s.candidate_day AS DATE) AS source_day,
     LOWER(TO_HEX(SHA256(TO_JSON_STRING(STRUCT(s.`GKGRECORDID` AS `GKGRECORDID`, s.`DATE` AS `DATE`, s.`SourceCollectionIdentifier` AS `SourceCollectionIdentifier`, s.`SourceCommonName` AS `SourceCommonName`, s.`DocumentIdentifier` AS `DocumentIdentifier`, s.`Themes` AS `Themes`, s.`V2Themes` AS `V2Themes`, s.`Persons` AS `Persons`, s.`V2Persons` AS `V2Persons`, s.`Organizations` AS `Organizations`, s.`V2Organizations` AS `V2Organizations`, s.`Locations` AS `Locations`, s.`V2Locations` AS `V2Locations`, s.`TranslationInfo` AS `TranslationInfo`, CAST(NULL AS STRING) AS `Title`, s.`ExtrasXML` AS `ExtrasXML`))))) AS raw_revision_sha256
   FROM
     (WITH
        narrow_source AS
        (SELECT
           CAST(g.`GKGRECORDID` AS STRING) AS `GKGRECORDID`,
           CAST(g.`DATE` AS STRING) AS `DATE`,
           CAST(g.`SourceCollectionIdentifier` AS STRING) AS `SourceCollectionIdentifier`,
           CAST(g.`DocumentIdentifier` AS STRING) AS `DocumentIdentifier`,
           CAST(DATE(g._PARTITIONTIME) AS STRING) AS candidate_day
         FROM `gdelt-bq.gdeltv2.gkg_partitioned` AS g
         WHERE g._PARTITIONTIME>=TIMESTAMP(@partition_start)
           AND g._PARTITIONTIME<TIMESTAMP(@partition_end)
           AND CAST(DATE(g._PARTITIONTIME) AS STRING) IN UNNEST(@eligible_days)),
        parsed_keys AS
        (SELECT
           s.`GKGRECORDID` AS `GKGRECORDID`,
           s.`DATE` AS `DATE`,
           s.`SourceCollectionIdentifier` AS `SourceCollectionIdentifier`,
           s.`DocumentIdentifier` AS `DocumentIdentifier`,
           s.candidate_day AS candidate_day,
           SAFE.PARSE_TIMESTAMP('%Y%m%d%H%M%S', s.`DATE`, 'UTC') AS source_date_utc,
           SAFE.PARSE_TIMESTAMP('%Y%m%d%H%M%S', REGEXP_EXTRACT(s.`GKGRECORDID`, r'^([0-9]{14})-T?[0-9]+$'), 'UTC') AS source_record_date_utc
         FROM narrow_source AS s
         WHERE SAFE_CAST(s.`SourceCollectionIdentifier` AS INT64)=@source_collection
           AND COALESCE(TRIM(s.`GKGRECORDID`), '')!=''
           AND COALESCE(TRIM(s.`DocumentIdentifier`), '')!=''
           AND REGEXP_CONTAINS(TRIM(s.`DocumentIdentifier`), r'(?i)^https?://')),
        raw_url_days AS
        (SELECT
           p.candidate_day AS candidate_day,
           p.`SourceCollectionIdentifier` AS `SourceCollectionIdentifier`,
           p.`DocumentIdentifier` AS `DocumentIdentifier`,
           MIN(IF(p.`DATE`!='0'
                  AND p.source_date_utc IS NOT NULL
                  AND p.source_record_date_utc IS NOT NULL
                  AND p.source_date_utc=p.source_record_date_utc
                  AND CAST(DATE(p.source_date_utc) AS STRING)=p.candidate_day, p.`GKGRECORDID`, NULL)) AS first_source_record_id,
           COUNT(*) AS source_rows_for_raw_url_day,
           COUNT(DISTINCT p.`GKGRECORDID`) AS source_record_ids_for_raw_url_day
         FROM parsed_keys AS p
         GROUP BY
           p.candidate_day,
           p.`SourceCollectionIdentifier`,
           p.`DocumentIdentifier`),
        ordered_keys AS
        (SELECT
           r.candidate_day AS candidate_day,
           r.`SourceCollectionIdentifier` AS `SourceCollectionIdentifier`,
           r.`DocumentIdentifier` AS `DocumentIdentifier`,
           r.first_source_record_id AS first_source_record_id,
           r.source_rows_for_raw_url_day AS source_rows_for_raw_url_day,
           r.source_record_ids_for_raw_url_day AS source_record_ids_for_raw_url_day,
           LOWER(TO_HEX(SHA256(CONCAT(@sample_seed, '\n', TO_JSON_STRING(STRUCT(r.`SourceCollectionIdentifier` AS source_collection, r.`DocumentIdentifier` AS document_identifier)))))) AS candidate_order
         FROM raw_url_days AS r
         WHERE r.first_source_record_id IS NOT NULL),
        daily_candidates AS
        (SELECT
           r.candidate_day AS candidate_day,
           ARRAY_AGG(STRUCT(r.`SourceCollectionIdentifier` AS source_collection, r.`DocumentIdentifier` AS document_identifier, r.first_source_record_id AS first_source_record_id, r.candidate_order AS candidate_order, r.source_rows_for_raw_url_day AS source_rows_for_raw_url_day, r.source_record_ids_for_raw_url_day AS source_record_ids_for_raw_url_day)
                     ORDER BY r.candidate_order, r.`DocumentIdentifier`
                     LIMIT @candidate_limit) AS candidate_keys
         FROM ordered_keys AS r
         GROUP BY r.candidate_day),
        chosen_keys AS
        (SELECT
           d.candidate_day AS candidate_day,
           k.source_collection AS source_collection,
           k.document_identifier AS document_identifier,
           k.first_source_record_id AS first_source_record_id,
           k.candidate_order AS candidate_order,
           k.source_rows_for_raw_url_day AS source_rows_for_raw_url_day,
           k.source_record_ids_for_raw_url_day AS source_record_ids_for_raw_url_day
         FROM daily_candidates AS d
         CROSS JOIN UNNEST(d.candidate_keys) AS k),
        selected_revisions AS
        (SELECT DISTINCT
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
           CAST(g.`Extras` AS STRING) AS `ExtrasXML`,
           k.candidate_day AS candidate_day,
           k.candidate_order AS candidate_order,
           k.source_rows_for_raw_url_day AS source_rows_for_raw_url_day,
           k.source_record_ids_for_raw_url_day AS source_record_ids_for_raw_url_day
         FROM `gdelt-bq.gdeltv2.gkg_partitioned` AS g
         JOIN chosen_keys AS k ON CAST(g.`SourceCollectionIdentifier` AS STRING)=k.source_collection
         AND CAST(g.`DocumentIdentifier` AS STRING)=k.document_identifier
         AND CAST(g.`GKGRECORDID` AS STRING)=k.first_source_record_id
         AND CAST(DATE(g._PARTITIONTIME) AS STRING)=k.candidate_day
         AND CAST(g.`DATE` AS STRING)=SUBSTR(k.first_source_record_id, 1, 14)
         WHERE g._PARTITIONTIME>=TIMESTAMP(@partition_start)
           AND g._PARTITIONTIME<TIMESTAMP(@partition_end)
           AND CAST(DATE(g._PARTITIONTIME) AS STRING) IN UNNEST(@eligible_days)) SELECT
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
        r.`ExtrasXML` AS `ExtrasXML`,
        r.candidate_day AS candidate_day,
        r.candidate_order AS candidate_order,
        r.source_rows_for_raw_url_day AS source_rows_for_raw_url_day,
        r.source_record_ids_for_raw_url_day AS source_record_ids_for_raw_url_day
      FROM selected_revisions AS r) AS s) AS s ON t.raw_revision_sha256=s.raw_revision_sha256
AND t.source_day=s.source_day
AND t.source_day>=DATE(TIMESTAMP(@partition_start))
AND t.source_day<DATE(TIMESTAMP(@partition_end)) WHEN NOT MATCHED THEN
INSERT (
          `GKGRECORDID`,
          `DATE`,
          `SourceCollectionIdentifier`,
          `SourceCommonName`,
          `DocumentIdentifier`,
          `Themes`,
          `V2Themes`,
          `Persons`,
          `V2Persons`,
          `Organizations`,
          `V2Organizations`,
          `Locations`,
          `V2Locations`,
          `TranslationInfo`,
          `Title`,
          `ExtrasXML`,
          `candidate_day`,
          `candidate_order`,
          `source_rows_for_raw_url_day`,
          `source_record_ids_for_raw_url_day`,
          `batch_month`,
          `source_day`,
          `raw_revision_sha256`)
VALUES (s.`GKGRECORDID`,s.`DATE`,s.`SourceCollectionIdentifier`,s.`SourceCommonName`,s.`DocumentIdentifier`,s.`Themes`,s.`V2Themes`,s.`Persons`,s.`V2Persons`,s.`Organizations`,s.`V2Organizations`,s.`Locations`,s.`V2Locations`,s.`TranslationInfo`,s.`Title`,s.`ExtrasXML`,s.`candidate_day`,s.`candidate_order`,s.`source_rows_for_raw_url_day`,s.`source_record_ids_for_raw_url_day`,s.`batch_month`,s.`source_day`,s.`raw_revision_sha256`);
