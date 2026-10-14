from __future__ import annotations

import pytest

from horizon.desktop_mcp.snapshots import SnapshotError, SnapshotStore


def _put(store: SnapshotStore, text: str = "abc", *, digest: str = "d1"):
    return store.put(
        root_key="root-1",
        relative_path="notes.txt",
        source="C:/approved/notes.txt",
        kind="txt",
        text=text,
        digest=digest,
        byte_count=len(text.encode("utf-8")),
        identity="volume:file-id",
    )


def test_snapshot_ids_are_opaque_versions_are_bound_to_digest_and_bytes_are_exact() -> None:
    store = SnapshotStore()
    first = _put(store, "old text", digest="old-digest")
    second = _put(store, "new text", digest="new-digest")

    assert first.source_id.startswith("cs1_")
    assert first.source_id != second.source_id
    assert first.version != second.version
    assert store.get(first.source_id).text == "old text"
    assert store.get(second.source_id).text == "new text"
    assert store.stats()["snapshot_source_bytes"] == len("old text") + len("new text")


def test_snapshot_expiry_is_enforced_using_monotonic_time() -> None:
    now = [100.0]
    store = SnapshotStore(ttl_seconds=10, clock=lambda: now[0])
    snapshot = _put(store)
    now[0] = 110.0

    with pytest.raises(SnapshotError) as caught:
        store.get(snapshot.source_id)
    assert caught.value.code == "snapshot_unknown"
    assert store.stats()["snapshot_count"] == 0
    assert store.stats()["snapshot_source_bytes"] == 0


def test_byte_and_count_quotas_evict_oldest_snapshot_and_keep_accounting_exact() -> None:
    store = SnapshotStore(max_snapshot_bytes=4, max_store_bytes=6, max_snapshots=2)
    first = _put(store, "abc", digest="one")
    second = _put(store, "def", digest="two")
    # The first lookup makes it newer than the second in the LRU order.
    store.get(first.source_id)
    third = _put(store, "ghi", digest="three")

    with pytest.raises(SnapshotError) as caught:
        store.get(second.source_id)
    assert caught.value.code == "snapshot_unknown"
    assert store.get(first.source_id).text == "abc"
    assert store.get(third.source_id).text == "ghi"
    assert store.stats()["snapshot_source_bytes"] == 6
    assert store.stats()["snapshot_count"] == 2


def test_snapshot_rejects_wrong_byte_count_and_per_snapshot_oversize() -> None:
    store = SnapshotStore(max_snapshot_bytes=3, max_store_bytes=6)
    with pytest.raises(SnapshotError, match="snapshot_invalid"):
        store.put(
            root_key="root-1",
            relative_path="notes.txt",
            source="C:/approved/notes.txt",
            kind="txt",
            text="abc",
            digest="digest",
            byte_count=2,
            identity="file-id",
        )
    with pytest.raises(SnapshotError) as caught:
        _put(store, "four")
    assert caught.value.code == "snapshot_too_large"


def test_clear_revokes_all_handles_and_releases_usage() -> None:
    store = SnapshotStore()
    snapshot = _put(store)
    store.clear()

    assert store.stats()["snapshot_count"] == 0
    assert store.stats()["snapshot_source_bytes"] == 0
    with pytest.raises(SnapshotError) as caught:
        store.get(snapshot.source_id)
    assert caught.value.code == "snapshot_unknown"
