ASSERT
  (SELECT COUNT(*)=2000000
   AND COUNT(DISTINCT document_id)=2000000
   AND COUNT(DISTINCT corpus_rank)=2000000
   AND MIN(corpus_rank)=1
   AND MAX(corpus_rank)=2000000
   AND COUNTIF(document_id IS NULL
               OR document_id=''
               OR observed_day IS NULL
               OR metadata_text IS NULL
               OR metadata_text=''
               OR BYTE_LENGTH(metadata_text)>6000
               OR metadata_sha256 IS DISTINCT
               FROM LOWER(TO_HEX(SHA256(metadata_text))))=0
   FROM `${dataset}.corpus_documents`) AS 'A complete validated TWO-million filtered corpus is required';
