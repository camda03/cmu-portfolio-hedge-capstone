-- Preserve the original allocation. Change only the 10,034 unavailable slots.

CREATE TABLE `${dataset}.daily_quotas_before_recovery_v01` AS
SELECT *
FROM `${dataset}.daily_quotas`;


CREATE TEMP TABLE capacity AS WITH counts AS
  (SELECT
     observed_day,
     COUNT(*) AS available
   FROM `${dataset}.candidate_representatives`
   GROUP BY observed_day)
SELECT
  q.*,
  DATE_TRUNC(q.observed_day, MONTH) AS month,
  COALESCE(c.available, 0) AS available,
  LEAST(q.target_documents, COALESCE(c.available, 0)) AS retained
FROM `${dataset}.daily_quotas_before_recovery_v01` AS q
LEFT JOIN counts AS c USING(observed_day);

ASSERT
  (SELECT COUNT(*)=3626
   AND COUNT(DISTINCT observed_day)=3626
   AND SUM(target_documents)=2000000
   AND SUM(available)=4990565
   AND SUM(target_documents-retained)=10034
   AND COUNTIF(available=0)=18
   AND COUNTIF(available<target_documents
               AND available!=0)=0
   FROM capacity) AS 'Reviewed candidate counts or deficits changed; original quotas remain in place';

ASSERT NOT EXISTS
  (SELECT 1
   FROM capacity AS c
   LEFT JOIN `${dataset}.recovery_source_gaps_v01` AS g USING(observed_day)
   WHERE (c.available=0) IS DISTINCT
     FROM (g.observed_day IS NOT NULL)) AS 'Candidate gaps must agree exactly with the verified source gaps';


CREATE TEMP TABLE monthly AS
SELECT
  month,
  SUM(target_documents-retained) AS missing,
  SUM(available-retained) AS spare
FROM capacity
GROUP BY month;

ASSERT NOT EXISTS
  (SELECT 1
   FROM monthly
   WHERE spare<missing) AS 'Insufficient qualifying capacity in a month; no cross-month substitution is permitted';

-- Bounded round-robin extra slots, with the existing seeded day order breaking ties.

CREATE TABLE `${dataset}.quota_reallocation_v01` AS WITH
  slots AS
  (SELECT
     c.observed_day,
     m.missing,
     ROW_NUMBER() OVER(PARTITION BY c.month
                       ORDER BY extra, c.stratum_order, c.observed_day) AS slot_rank
   FROM capacity AS c
   JOIN monthly AS m USING(month)
   CROSS JOIN UNNEST(GENERATE_ARRAY(1, LEAST(c.available-c.retained, m.missing))) AS extra),
  additions AS
  (SELECT
     observed_day,
     COUNT(*) AS extra
   FROM slots
   WHERE slot_rank<=missing
   GROUP BY observed_day)
SELECT
  c.observed_day,
  c.month,
  c.stratum_order,
  c.target_documents AS original_target,
  c.available,
  c.retained+COALESCE(a.extra, 0) AS effective_target
FROM capacity AS c
LEFT JOIN additions AS a USING(observed_day);

ASSERT
  (SELECT COUNT(*)=3626
   AND SUM(effective_target)=2000000
   AND SUM(GREATEST(effective_target-original_target, 0))=10034
   AND COUNTIF(effective_target<0
               OR effective_target>available)=0
   FROM `${dataset}.quota_reallocation_v01`) AS 'Allocation must conserve two million slots and respect actual daily capacity';

ASSERT NOT EXISTS
  (SELECT month
   FROM `${dataset}.quota_reallocation_v01`
   GROUP BY month
   HAVING SUM(original_target)!=SUM(effective_target)) AS 'Monthly allocation totals changed';

-- Independently recompute checkpoint expectations using the existing priority rule.

CREATE TEMP TABLE checkpoint_counts AS WITH slots AS
  (SELECT
     q.observed_day,
     ROW_NUMBER() OVER(
                       ORDER BY SAFE_DIVIDE(CAST(2*k-1 AS BIGNUMERIC), CAST(2*q.effective_target AS BIGNUMERIC)), q.stratum_order, q.observed_day) AS position
   FROM `${dataset}.quota_reallocation_v01` AS q
   CROSS JOIN UNNEST(GENERATE_ARRAY(1, q.effective_target)) AS k)
SELECT
  observed_day,
  COUNTIF(position<=100000) AS at100k,
  COUNTIF(position<=1000000) AS at1m
FROM slots
GROUP BY observed_day;


CREATE TABLE `${dataset}.recovery_quota_manifest_v01` AS
SELECT
  @policy_json AS policy_json,
  LOWER(TO_HEX(SHA256(STRING_AGG(TO_JSON_STRING(STRUCT(q.observed_day, q.original_target, q.effective_target, COALESCE(c.at100k, 0) AS checkpoint_100k, COALESCE(c.at1m, 0) AS checkpoint_1m)), '\n'
                                 ORDER BY q.observed_day)))) AS allocation_sha256,
  COUNT(*) AS planned_days,
  COUNTIF(q.effective_target>0) AS represented_days,
  SUM(q.effective_target) AS document_target,
  SUM(GREATEST(q.effective_target-q.original_target, 0)) AS reassigned_slots,
  CURRENT_TIMESTAMP() AS recorded_at
FROM `${dataset}.quota_reallocation_v01` AS q
LEFT JOIN checkpoint_counts AS c USING(observed_day);

BEGIN TRANSACTION;


UPDATE `${dataset}.daily_quotas` AS d
SET
  target_documents=q.effective_target,
  checkpoint_100k_target=COALESCE(c.at100k, 0),
  checkpoint_1m_target=COALESCE(c.at1m, 0)
FROM `${dataset}.quota_reallocation_v01` AS q
LEFT JOIN checkpoint_counts AS c USING(observed_day)
WHERE d.observed_day=q.observed_day;

ASSERT
  (SELECT COUNT(*)=3626
   AND COUNTIF(target_documents>0)=3608
   AND SUM(target_documents)=2000000
   AND SUM(checkpoint_100k_target)=100000
   AND SUM(checkpoint_1m_target)=1000000
   FROM `${dataset}.daily_quotas`) AS 'Revised allocation/checkpoints failed; transaction rolls back';


COMMIT TRANSACTION;
