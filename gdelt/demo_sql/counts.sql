SELECT
  COUNTIF(NOT cached) AS new_inputs,
  COUNTIF(cached) AS cached_inputs
FROM `${query_requests}`;
