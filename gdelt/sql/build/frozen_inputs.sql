BEGIN TRANSACTION;

ASSERT
  (SELECT COUNT(*)
   FROM `${dataset}.build_contract`)=0 AS 'Build contract table must be empty';

ASSERT
  (SELECT COUNT(*)
   FROM `${dataset}.daily_quotas`)=0 AS 'Daily quotas must be empty';

ASSERT
  (SELECT COUNT(*)
   FROM `${dataset}.market_closes`)=0 AS 'Market closes must be empty';


INSERT INTO `${dataset}.build_contract` (run_id, contract_sha256, contract_json, created_at)
VALUES (@run_id,@contract_sha256,@contract_json,CURRENT_TIMESTAMP());


INSERT INTO `${dataset}.daily_quotas` (observed_day, target_documents, stratum_order, checkpoint_100k_target, checkpoint_1m_target)
SELECT
  CAST(JSON_VALUE(q, '$.day') AS DATE) AS observed_day,
  CAST(JSON_VALUE(q, '$.target_unique_documents') AS INT64) AS target_documents,
  LOWER(TO_HEX(SHA256(TO_JSON_STRING([@sample_seed,'stratum-order',JSON_VALUE(q,'$.day')])))) AS stratum_order,
  CAST(JSON_VALUE(q, '$.checkpoint_targets."100000"') AS INT64) AS checkpoint_100k_target,
  CAST(JSON_VALUE(q, '$.checkpoint_targets."1000000"') AS INT64) AS checkpoint_1m_target
FROM UNNEST(JSON_QUERY_ARRAY(@source_plan_json, '$.planned_source_windows')) AS q;


INSERT INTO `${dataset}.market_closes` (session_date, close_price, available_at)
SELECT
  m.session_date,
  m.close_price,
  m.available_at
FROM `${baseline_dataset}.market_closes` AS m;

ASSERT
  (SELECT COUNT(*)=COUNT(DISTINCT observed_day)
   AND SUM(target_documents)=@target_documents
   AND MIN(target_documents)>0
   AND MAX(target_documents)<=1000000
   FROM `${dataset}.daily_quotas`) AS 'Daily quota contract differs';

ASSERT
  (SELECT COUNT(*)=@market_observations
   AND COUNT(*)=COUNT(DISTINCT session_date)
   AND COUNTIF(close_price IS NULL
               OR close_price<=0
               OR IS_NAN(close_price)
               OR IS_INF(close_price)
               OR available_at>TIMESTAMP(@as_of_utc))=0
   FROM `${dataset}.market_closes`) AS 'Market close contract differs';


COMMIT TRANSACTION;
