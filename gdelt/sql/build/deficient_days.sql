SELECT
  q.observed_day,
  q.target_documents,
  COALESCE(c.available, 0) AS available,
  GREATEST(q.target_documents-COALESCE(c.available, 0), 0) AS missing_documents
FROM `${dataset}.daily_quotas` AS q
LEFT JOIN
  (SELECT
     observed_day,
     COUNT(*) AS available
   FROM `${dataset}.candidate_representatives`
   GROUP BY observed_day) AS c ON c.observed_day=q.observed_day
WHERE COALESCE(c.available, 0)<q.target_documents
ORDER BY q.observed_day;
