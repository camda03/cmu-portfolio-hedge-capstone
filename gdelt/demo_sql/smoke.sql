-- One public, synthetic input checks current permissions/model compatibility before a large build.

CREATE TEMP TABLE model_smoke AS
SELECT AI.EMBED('Public synthetic capstone test: monetary policy and inflation.', endpoint=>'gemini-embedding-2', model_params=>JSON '{"outputDimensionality":3072,"autoTruncate":false}') AS outcome;

ASSERT
  (SELECT COUNT(*)=1
   AND COUNTIF(outcome.status=''
               AND ARRAY_LENGTH(outcome.result)=3072
               AND NOT EXISTS
                 (SELECT 1
                  FROM UNNEST(outcome.result) x
                  WHERE x IS NULL
                    OR IS_NAN(x)
                    OR IS_INF(x))
               AND
                 (SELECT MAX(ABS(x))
                  FROM UNNEST(outcome.result) x)>0)=1
   FROM model_smoke) AS 'Model/permission smoke test failed; inspect the saved job before building the corpus';


SELECT
  outcome.status AS status,
  ARRAY_LENGTH(outcome.result) AS dimensions
FROM model_smoke
LIMIT 1;
