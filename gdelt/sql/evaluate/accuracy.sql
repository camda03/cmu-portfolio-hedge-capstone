-- Table identifiers are validated template inputs; scalar inputs are typed query parameters.

CREATE TEMP TABLE evaluation_status AS WITH
  per_query AS
  (SELECT
     e.query_document_id AS query_document_id,
     COUNT(*) AS exact_pairs,
     COUNTIF(a.historical_document_id IS NOT NULL) AS overlap_pairs,
     COUNTIF(e.cosine_rank=1
             AND a.cosine_rank=1) AS top1_agreement,
     MIN(IF(e.cosine_rank=1, e.cosine_distance, NULL)) AS exact_best
   FROM `${dataset}.full2m_eval_exact_matches_v01` AS e
   LEFT JOIN `${dataset}.full2m_eval_ivf40_matches_v01` AS a ON a.query_document_id=e.query_document_id
   AND a.historical_document_id=e.historical_document_id
   GROUP BY e.query_document_id),
  exact_bounds AS
  (SELECT
     e.query_document_id AS query_document_id,
     MIN(e.cosine_distance) AS exact_best,
     MAX(e.cosine_distance) AS exact_boundary
   FROM `${dataset}.full2m_eval_exact_matches_v01` AS e
   GROUP BY e.query_document_id),
  exact_ties AS
  (SELECT
     e.query_document_id AS query_document_id,
     COUNTIF(ABS(e.cosine_distance-b.exact_best)<=1e-10)>1 AS top1_tied,
     COUNTIF(ABS(e.cosine_distance-b.exact_boundary)<=1e-10)>1 AS cutoff_tied
   FROM `${dataset}.full2m_eval_exact_matches_v01` AS e
   JOIN exact_bounds AS b ON b.query_document_id=e.query_document_id
   GROUP BY e.query_document_id),
  ann_best AS
  (SELECT
     a.query_document_id AS query_document_id,
     MIN(a.cosine_distance) AS ann_best,
     COUNTIF(a.cosine_distance<=b.exact_boundary+1e-10) AS distance_acceptable_pairs
   FROM `${dataset}.full2m_eval_ivf40_matches_v01` AS a
   JOIN exact_bounds AS b ON b.query_document_id=a.query_document_id
   GROUP BY a.query_document_id)
SELECT
  COUNT(*) AS query_documents,

  (SELECT COUNT(DISTINCT r.query_request_id)
   FROM `${dataset}.full2m_eval_reference_documents_v01` AS r) AS sample_unique_requests,

  (SELECT COUNT(*)
   FROM `${query_documents}`) AS evaluated_query_documents,
  SUM(p.exact_pairs) AS exact_pairs,

  (SELECT COUNT(*)
   FROM `${dataset}.full2m_eval_ivf40_matches_v01` AS a
   JOIN `${dataset}.full2m_eval_reference_documents_v01` AS r ON r.query_document_id=a.query_document_id) AS ann_pairs,
  SUM(p.overlap_pairs) AS overlap_pairs,
  AVG(p.overlap_pairs/20.0) AS mean_recall_at_20,
  MIN(p.overlap_pairs/20.0) AS minimum_recall_at_20,
  APPROX_QUANTILES(p.overlap_pairs/20.0, 100)[SAFE_OFFSET(5)] AS p05_recall_at_20,
  APPROX_QUANTILES(p.overlap_pairs/20.0, 100)[SAFE_OFFSET(50)] AS p50_recall_at_20,
  COUNTIF(p.overlap_pairs=20) AS full_recall_queries,
  SUM(p.top1_agreement) AS top1_identity_agreement_queries,
  AVG(CAST(p.top1_agreement AS FLOAT64)) AS top1_identity_agreement_rate,
  AVG(a.ann_best-p.exact_best) AS mean_best_distance_gap,
  MAX(a.ann_best-p.exact_best) AS maximum_best_distance_gap,
  COUNTIF(p.overlap_pairs<16) AS queries_below_80pct_recall,
  COUNTIF(p.overlap_pairs<19) AS queries_below_95pct_recall,
  COUNTIF(ABS(a.ann_best-p.exact_best)<=1e-10) AS top1_distance_agreement_queries,
  AVG(IF(ABS(a.ann_best-p.exact_best)<=1e-10, 1.0, 0.0)) AS top1_distance_agreement_rate,
  AVG(a.distance_acceptable_pairs/20.0) AS mean_distance_tolerant_recall_at_20,
  COUNTIF(t.top1_tied) AS queries_with_top1_ties_in_exact20,
  COUNTIF(t.cutoff_tied) AS queries_with_cutoff_ties_in_exact20,
  1e-10 AS comparison_distance_tolerance
FROM per_query AS p
JOIN ann_best AS a ON a.query_document_id=p.query_document_id
JOIN exact_ties AS t ON t.query_document_id=p.query_document_id
LIMIT 1;

ASSERT
  (SELECT query_documents=
     (SELECT COUNT(*)
      FROM `${dataset}.full2m_eval_reference_documents_v01`)
   AND query_documents>0
   AND sample_unique_requests=query_documents
   AND exact_pairs=20*query_documents
   AND ann_pairs=exact_pairs
   AND mean_recall_at_20 BETWEEN 0 AND 1
   AND minimum_recall_at_20 BETWEEN 0 AND 1
   AND mean_distance_tolerant_recall_at_20 BETWEEN 0 AND 1
   AND top1_identity_agreement_rate BETWEEN 0 AND 1
   AND top1_distance_agreement_rate BETWEEN 0 AND 1
   AND (NOT @exclude_identical_inputs
        OR (mean_recall_at_20>=@minimum_mean_recall
            AND minimum_recall_at_20>=@minimum_query_recall))
   FROM evaluation_status) AS 'Exact-reference scope and recall metrics must be valid; filtered mode must meet its engineering thresholds';


SELECT
  query_documents,
  sample_unique_requests,
  evaluated_query_documents,
  exact_pairs,
  ann_pairs,
  overlap_pairs,
  mean_recall_at_20,
  minimum_recall_at_20,
  p05_recall_at_20,
  p50_recall_at_20,
  full_recall_queries,
  top1_identity_agreement_queries,
  top1_identity_agreement_rate,
  mean_best_distance_gap,
  maximum_best_distance_gap,
  queries_below_80pct_recall,
  queries_below_95pct_recall,
  top1_distance_agreement_queries,
  top1_distance_agreement_rate,
  mean_distance_tolerant_recall_at_20,
  queries_with_top1_ties_in_exact20,
  queries_with_cutoff_ties_in_exact20,
  comparison_distance_tolerance
FROM evaluation_status
LIMIT 1;
