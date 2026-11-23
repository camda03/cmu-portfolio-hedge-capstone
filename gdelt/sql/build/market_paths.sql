CREATE OR REPLACE TABLE `${dataset}.market_paths` CLUSTER BY observed_day AS WITH
  sessions AS
  (SELECT
     m.session_date AS session_date,
     m.close_price AS close_price,
     ROW_NUMBER() OVER (
                        ORDER BY m.session_date) AS session_index
   FROM `${dataset}.market_closes` AS m),
  anchors AS
  (SELECT
     q.observed_day AS observed_day,
     MIN(s.session_index) AS anchor_index
   FROM `${dataset}.daily_quotas` AS q
   JOIN sessions AS s ON s.session_date>q.observed_day  -- Start at the first close after the news date.
   GROUP BY q.observed_day),
  paths AS
  (SELECT
     a.observed_day AS observed_day,
     ARRAY_AGG(STRUCT(s.session_index-a.anchor_index AS horizon_sessions, s.session_date AS session_date, s.close_price AS close_price)
               ORDER BY s.session_index) AS observed_path
   FROM anchors AS a
   JOIN sessions AS s ON s.session_index BETWEEN a.anchor_index AND a.anchor_index+20
   GROUP BY a.observed_day)
SELECT
  p.observed_day AS observed_day,
  p.observed_path[SAFE_OFFSET(0)].session_date AS anchor_date,
  p.observed_path[SAFE_OFFSET(20)].session_date AS day20_date,
  ARRAY_LENGTH(p.observed_path) AS price_observations,
  ARRAY_LENGTH(p.observed_path)=21
AND
  TIMESTAMP_ADD(TIMESTAMP(p.observed_day),INTERVAL 1 DAY)<=TIMESTAMP(@as_of_utc) AS eligible,
  ARRAY
  (SELECT AS STRUCT x.horizon_sessions AS horizon_sessions, x.session_date AS session_date, x.close_price AS close_price, SAFE_DIVIDE(x.close_price, p.observed_path[SAFE_OFFSET(0)].close_price)-1 AS price_return
   FROM UNNEST(p.observed_path) AS x
   ORDER BY x.horizon_sessions) AS forward_path
FROM paths AS p;
