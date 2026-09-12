# Property-based tests

Hypothesis property tests validating the 28 design correctness properties.

Conventions:

- One property == one test, marked `@pytest.mark.property`.
- Minimum **100 iterations** per property (`@settings(max_examples=100)`).
- Tagged with a comment: `Feature: clinic-front-desk-agent, Property N: <text>`.
- Annotated with `**Validates: Requirements X.Y**`.
- Run against the in-memory fake stores by default (Property 27 additionally
  runs against DynamoDB-local).
