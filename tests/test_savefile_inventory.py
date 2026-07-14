from ermlib.savefile import SaveFile


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
