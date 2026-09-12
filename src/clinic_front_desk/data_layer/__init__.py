"""Data_Layer: swappable, provider-aware data-access interfaces.

Agents and tools depend only on the abstract interfaces here, never on storage
directly (Req 16.1). Sub-packages:

- :mod:`clinic_front_desk.data_layer.interfaces` — the seven abstract store
  interfaces and the ``Result`` contract.
- :mod:`clinic_front_desk.data_layer.memory` — in-memory fake stores used as the
  default backend for property tests.
- :mod:`clinic_front_desk.data_layer.dynamodb` — DynamoDB single-table
  implementations.
- :mod:`clinic_front_desk.data_layer.events` — ``ChangeEvent`` and the
  change-emitter hook invoked on every successful mutation.
"""
