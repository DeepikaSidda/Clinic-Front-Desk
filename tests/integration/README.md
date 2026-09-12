# Integration tests

Cross-component / infrastructure tests, marked `@pytest.mark.integration`:
real-time propagation budgets (config-change, escalation, decision feed,
schedule/activity) and the storage-swap equivalence run against DynamoDB-local
vs. the in-memory fake (Property 27 / Req 16.5).
