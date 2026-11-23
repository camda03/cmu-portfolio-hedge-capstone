ASSERT
  (SELECT planned_days=3626
   AND eligible_days=3626
   AND target_documents=2000000
   FROM
     (SELECT COUNT(*) AS planned_days, COUNTIF(m.eligible IS TRUE) AS eligible_days, SUM(q.target_documents) AS target_documents, COUNT(DISTINCT m.anchor_date) AS distinct_market_anchors
      FROM `${dataset}.daily_quotas` AS q
      LEFT JOIN `${dataset}.market_paths` AS m ON m.observed_day=q.observed_day
      LIMIT 1)) AS 'Frozen market calendar differs';
