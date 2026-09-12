"""Shared plumbing for the DynamoDB single-table store implementations (task 4.1).

Every DynamoDB store shares one table and the same observable contract as its
in-memory fake counterpart (:mod:`clinic_front_desk.data_layer.memory`):

- **Empty initialization (Req 16.4).** Reads return empty results until an item
  is written — inherent to DynamoDB (a query/get against a fresh table returns
  nothing).
- **Atomicity / non-destruction (Req 16.6).** Each write validates *before*
  mutating any item, so a rejected write leaves every prior record unchanged.
  The single-item writes (put/delete) are inherently atomic; multi-item writes
  (reschedule, cancel, decision status change) validate fully first, then apply.
- **Change emission on success only (Req 16.6).** A store emits a
  :class:`~clinic_front_desk.data_layer.events.ChangeEvent` through its injected
  :class:`~clinic_front_desk.data_layer.events.ChangeEmitter` *after* — and only
  after — the write has committed. A failed write emits nothing.
- **Provider-id enforcement (Req 16.3, 16.7).** Schedule-owning writes reject a
  missing/blank ``provider_id`` with a ``validation`` :class:`StoreError`.

The Decimal boundary
--------------------
The :mod:`clinic_front_desk.models.dynamo` (de)serialization helpers produce and
consume *plain* JSON-compatible Python values (``str``/``int``/``float``/
``bool``/``None``/``list``/``dict``). The boto3 resource API, however, rejects
``float`` and represents every number as :class:`decimal.Decimal`. :func:`to_dynamo`
converts plain numbers to ``Decimal`` on the way *in* and :func:`from_dynamo`
converts ``Decimal`` back to ``int``/``float`` on the way *out*, so the rest of
the code never sees a ``Decimal`` and round-trips exactly.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from boto3.dynamodb.conditions import Attr, Key  # type: ignore[import-untyped]

from clinic_front_desk.data_layer.events import (
    ChangeEmitter,
    ChangeEntity,
    ChangeEvent,
    ChangeKind,
    NullChangeEmitter,
)
from clinic_front_desk.models import Item

# Global-secondary-index names for the single-table layout (design "DynamoDB
# Table Design"). GSI1: open-slot lookup; GSI2: appointments by patient;
# GSI3: patient name+phone lookup; GSI4: decision finding-key dedupe.
GSI1 = "GSI1"
GSI2 = "GSI2"
GSI3 = "GSI3"
GSI4 = "GSI4"


# ---------------------------------------------------------------------------
# Decimal boundary conversion
# ---------------------------------------------------------------------------


def to_dynamo(value: Any) -> Any:
    """Recursively convert plain values to their boto3-resource representation.

    ``float`` becomes :class:`decimal.Decimal` (via ``str`` so the decimal value
    matches the human-readable float, not its binary expansion). ``bool`` is
    preserved (it must be handled before ``int`` since ``bool`` is a subclass of
    ``int``). Everything else passes through; ``int`` is accepted by the resource
    API as-is and ``None`` maps to the DynamoDB ``NULL`` type.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: to_dynamo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_dynamo(v) for v in value]
    return value


def from_dynamo(value: Any) -> Any:
    """Recursively convert a boto3-resource item back to plain Python values.

    Every DynamoDB number is returned by boto3 as :class:`decimal.Decimal`; an
    integral value becomes ``int`` and a fractional value becomes ``float`` so
    the reconstructed entities carry the same plain types they were built from.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    if isinstance(value, dict):
        return {k: from_dynamo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [from_dynamo(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Store base
# ---------------------------------------------------------------------------


class DynamoStoreBase:
    """Common state and helpers for the single-table DynamoDB stores.

    Holds the shared boto3 ``Table`` resource and the change emitter, plus thin
    put/get/query/scan wrappers that cross the Decimal boundary. Not a store on
    its own.
    """

    def __init__(self, table: Any, emitter: ChangeEmitter | None = None) -> None:
        # ``table`` is a boto3 DynamoDB ``Table`` resource (typed ``Any`` because
        # boto3 ships no type stubs). Default to a no-op emitter so stores can
        # always assume one is present (Req 16.6).
        self._table = table
        self._emitter: ChangeEmitter = emitter or NullChangeEmitter()

    # -- change emission ---------------------------------------------------

    def _emit(self, entity: ChangeEntity, id: str, kind: ChangeKind) -> None:
        """Publish a successful-mutation event (Req 16.6)."""
        self._emitter.emit(ChangeEvent(entity=entity, id=id, kind=kind))

    # -- item-level helpers (cross the Decimal boundary) -------------------

    def _put(self, item: Item) -> None:
        """Write a plain item, converting numbers to ``Decimal`` for boto3."""
        self._table.put_item(Item=to_dynamo(item))

    def _put_many(self, items: list[Item]) -> None:
        """Write many items through a batch writer.

        Publishing a year of appointment slots is thousands of items, and one
        ``PutItem`` round trip each turns that into minutes of latency — long
        enough that the HTTP request publishing them times out. ``batch_writer``
        groups them 25 at a time and handles retries for unprocessed items.

        Not atomic: a failure part-way leaves earlier batches written. That is
        acceptable *only* because slot ids are deterministic, so re-running the
        publish converges rather than duplicating. Do not reuse this for writes
        that must be all-or-nothing (Req 16.6).
        """
        if not items:
            return
        with self._table.batch_writer() as batch:
            for item in items:
                batch.put_item(Item=to_dynamo(item))

    def _delete(self, pk: str, sk: str) -> None:
        """Delete the item with the given composite key."""
        self._table.delete_item(Key={"PK": pk, "SK": sk})

    def _delete_many(self, keys: list[tuple[str, str]]) -> None:
        """Delete many items by composite key through a batch writer.

        Same reasoning as :meth:`_put_many`: closing the out-of-hours slots across a
        published quarter is thousands of items, and a round trip each would take
        minutes. Not atomic — a failure part-way leaves earlier batches deleted.
        Acceptable only for deletes that are safe to re-run, which slot removal is:
        deleting an already-absent key is a no-op.
        """
        if not keys:
            return
        with self._table.batch_writer() as batch:
            for pk, sk in keys:
                batch.delete_item(Key={"PK": pk, "SK": sk})

    def _get(self, pk: str, sk: str) -> Item | None:
        """Return the plain item for a composite key, or ``None`` if absent."""
        resp = self._table.get_item(Key={"PK": pk, "SK": sk})
        item = resp.get("Item")
        if item is None:
            return None
        plain: Item = from_dynamo(item)
        return plain

    def _query(
        self,
        key_condition: Any,
        *,
        index_name: str | None = None,
        filter_expression: Any | None = None,
        scan_index_forward: bool = True,
    ) -> list[Item]:
        """Run a (paginated) query and return the plain items.

        ``key_condition``/``filter_expression`` are ``boto3.dynamodb.conditions``
        expressions. Results are converted back across the Decimal boundary.
        """
        return list(
            self._query_iter(
                key_condition,
                index_name=index_name,
                filter_expression=filter_expression,
                scan_index_forward=scan_index_forward,
            )
        )

    def _query_iter(
        self,
        key_condition: Any,
        *,
        index_name: str | None = None,
        filter_expression: Any | None = None,
        scan_index_forward: bool = True,
        page_size: int | None = None,
    ) -> Iterator[Item]:
        """Yield query results item by item, fetching pages only as needed.

        The eager :meth:`_query` reads every page before returning, which is right
        when the caller wants the whole set. A caller that only needs the first
        few results can stop consuming this and never pay for the rest — the
        difference between 3 slots and a year of them.

        ``page_size`` caps items read per request. Note DynamoDB applies it before
        ``filter_expression``, so a page can come back short (or empty) while more
        matches exist further on; that is why this yields until the pages run out
        rather than stopping on a short page.
        """
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": key_condition,
            "ScanIndexForward": scan_index_forward,
        }
        if index_name is not None:
            kwargs["IndexName"] = index_name
        if filter_expression is not None:
            kwargs["FilterExpression"] = filter_expression
        if page_size is not None:
            kwargs["Limit"] = page_size

        resp = self._table.query(**kwargs)
        while True:
            for item in resp.get("Items", []):
                yield from_dynamo(item)
            last_key = resp.get("LastEvaluatedKey")
            if last_key is None:
                return
            resp = self._table.query(ExclusiveStartKey=last_key, **kwargs)

    def _find_by_entity_id(self, entity: str, record_id: str) -> Item | None:
        """Locate a single item by its ``entity`` discriminator and ``id``.

        Several interface methods (``get``/``move``/``remove``/``set_status``/
        ``finalize``) take only a record id, but the single-table key for those
        entities is composite (it embeds provider/date/status/timestamps that the
        id alone does not carry). This scans with a filter to resolve the id to
        its stored item (including its ``PK``/``SK``) so the caller can then read
        or mutate it precisely. Data volumes for a solo-doctor clinic make this
        inexpensive, and it keeps the design's stated key schema unchanged.
        """
        filt = Attr("entity").eq(entity) & Attr("id").eq(record_id)
        items: list[Any] = []
        resp = self._table.scan(FilterExpression=filt)
        items.extend(resp.get("Items", []))
        while "LastEvaluatedKey" in resp:
            resp = self._table.scan(FilterExpression=filt, ExclusiveStartKey=resp["LastEvaluatedKey"])
            items.extend(resp.get("Items", []))
        if not items:
            return None
        plain: Item = from_dynamo(items[0])
        return plain


# ---------------------------------------------------------------------------
# Re-exported condition builders (so stores import from one place)
# ---------------------------------------------------------------------------

__all__ = [
    "GSI1",
    "GSI2",
    "GSI3",
    "GSI4",
    "Attr",
    "Key",
    "to_dynamo",
    "from_dynamo",
    "DynamoStoreBase",
    "create_table",
    "table_exists",
]


# ---------------------------------------------------------------------------
# Table bootstrap (PK/SK + GSI1-GSI4) for DynamoDB-local / moto
# ---------------------------------------------------------------------------

# Attribute definitions for every attribute used as a table or index key.
_ATTRIBUTE_DEFINITIONS = [
    {"AttributeName": "PK", "AttributeType": "S"},
    {"AttributeName": "SK", "AttributeType": "S"},
    {"AttributeName": "GSI1PK", "AttributeType": "S"},
    {"AttributeName": "GSI1SK", "AttributeType": "S"},
    {"AttributeName": "GSI2PK", "AttributeType": "S"},
    {"AttributeName": "GSI2SK", "AttributeType": "S"},
    {"AttributeName": "GSI3PK", "AttributeType": "S"},
    {"AttributeName": "GSI4PK", "AttributeType": "S"},
]

_KEY_SCHEMA = [
    {"AttributeName": "PK", "KeyType": "HASH"},
    {"AttributeName": "SK", "KeyType": "RANGE"},
]

_GLOBAL_SECONDARY_INDEXES = [
    {
        "IndexName": GSI1,
        "KeySchema": [
            {"AttributeName": "GSI1PK", "KeyType": "HASH"},
            {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
        ],
        "Projection": {"ProjectionType": "ALL"},
    },
    {
        "IndexName": GSI2,
        "KeySchema": [
            {"AttributeName": "GSI2PK", "KeyType": "HASH"},
            {"AttributeName": "GSI2SK", "KeyType": "RANGE"},
        ],
        "Projection": {"ProjectionType": "ALL"},
    },
    {
        "IndexName": GSI3,
        "KeySchema": [{"AttributeName": "GSI3PK", "KeyType": "HASH"}],
        "Projection": {"ProjectionType": "ALL"},
    },
    {
        "IndexName": GSI4,
        "KeySchema": [{"AttributeName": "GSI4PK", "KeyType": "HASH"}],
        "Projection": {"ProjectionType": "ALL"},
    },
]


def table_exists(dynamodb: Any, table_name: str) -> bool:
    """Return ``True`` if ``table_name`` already exists on the given resource."""
    client = dynamodb.meta.client
    try:
        client.describe_table(TableName=table_name)
        return True
    except client.exceptions.ResourceNotFoundException:
        return False


def create_table(dynamodb: Any, table_name: str) -> Any:
    """Create the single-table layout (PK/SK + GSI1-GSI4) and return the table.

    Usable against DynamoDB-local or moto. Uses on-demand billing so no
    provisioned throughput is required. If the table already exists it is
    returned as-is (idempotent bootstrap).

    Args:
        dynamodb: A boto3 DynamoDB *resource* (``boto3.resource("dynamodb", ...)``).
        table_name: The table to create.

    Returns:
        The boto3 ``Table`` resource, ready for use once active.
    """
    if table_exists(dynamodb, table_name):
        return dynamodb.Table(table_name)

    table = dynamodb.create_table(
        TableName=table_name,
        KeySchema=_KEY_SCHEMA,
        AttributeDefinitions=_ATTRIBUTE_DEFINITIONS,
        GlobalSecondaryIndexes=_GLOBAL_SECONDARY_INDEXES,
        BillingMode="PAY_PER_REQUEST",
    )
    table.wait_until_exists()
    return table
