# pro_a Community export v1

`EXPORT_CONTRACT_VERSION = zsxq-pro-a-community-export-v1`
`EXPORT_CONTRACT_SHA256 = 6a3e2dc77287d426b8afda46df9fed24b6188e27df16946a8e88b59d91d66d48`

The machine-readable contract is [pro_a_community_export_contract_v1.json](pro_a_community_export_contract_v1.json). A completed run writes `pro_a_export.zip` and exposes a manual web download. It never posts to pro_a. The existing read-only topic search, detail retrieval, DeepSeek analysis, and report generation remain the source pipeline.

The archive contains exactly `manifest.json` and `topics.jsonl`. Export rows join detail and analysis by exact normalized `topic_id`, reject duplicate IDs, and include only in-range topics marked reportable and materially relevant. Missing analysis is omitted and counted. Rows sort by UTC publication time descending, null dates last, then ID ascending. JSON serialization, ZIP member timestamps/order, and compression level are fixed. The logical bundle SHA binds the manifest without its own SHA followed by the topic bytes.

`evidence_text` comes only from existing sanitized search/detail content according to `analysis_source`. DeepSeek routing fields are confined to the separate `routing` object. Summary, impact, evidence points, risk notes, and verification items are never copied into evidence. Sensitive source keys and tokenized URLs/paths are redacted. The ZIP has no credential state or local absolute path.

Limits: 20 MiB ZIP, 100 topics, 12,000 evidence characters per topic, 500,000 total evidence characters. pro_a also enforces 40 MiB uncompressed and a 100:1 maximum per-member compression ratio.
