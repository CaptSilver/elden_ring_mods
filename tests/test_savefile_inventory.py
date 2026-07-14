import struct

from ermlib.savefile import SaveFile, _read_inventory_slot, _resolve


def test_slot0_stats_inventory_and_method_agreement(real_save_bytes):
    sf = SaveFile.from_bytes(real_save_bytes)
    sd = sf.slot_data(0, "Silverie")
    # two independent PGD-finding methods must agree — the strongest correctness proof
    assert sd.walk_cursor == sd.pgd_base
    assert sd.stats["intelligence"] == 80
    assert sd.stats["vigor"] == 80
    assert sd.level == 294
    assert sum(sd.stats.values()) - 79 == sd.level     # vanilla level invariant
    assert sd.corrupt_handles == 0
    assert 800 < len(sd.items) < 900                    # 856 occupied common items
    assert sd.common_count_field <= 0xA80


def test_slot1_also_parses(real_save_bytes):
    sf = SaveFile.from_bytes(real_save_bytes)
    sd = sf.slot_data(1, "Silverie")
    assert sd.walk_cursor == sd.pgd_base
    assert sd.level == 346
    assert sd.corrupt_handles == 0


def test_unknown_nibble_resolves_corrupt():
    # nibble 3 is not in {0,8,9,A,B,C} → corrupt handle
    assert _resolve(0x30000000, {}) == -1


def test_read_inventory_slot_counts_corrupt_handle():
    # a 12-byte {handle, qty, acq} record with an unknown-nibble handle
    body = struct.pack("<III", 0x30000000, 1, 0)
    item, is_corrupt = _read_inventory_slot(body, 0, {})
    assert item is None
    assert is_corrupt is True


def test_read_inventory_slot_empty_and_valid_not_corrupt():
    empty = struct.pack("<III", 0, 0, 0)
    item, is_corrupt = _read_inventory_slot(empty, 0, {})
    assert item is None and is_corrupt is False
    # nibble A resolves directly, not corrupt
    valid = struct.pack("<III", 0xA0000123, 5, 0)
    item, is_corrupt = _read_inventory_slot(valid, 0, {})
    assert is_corrupt is False
    assert item is not None and item.item_id == 0x123 and item.quantity == 5
