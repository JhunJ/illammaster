from app.services.beam_flat_frontend_match import compute_beam_horizontal_picked_server, SectionCandidate


def test_beam_horizontal_picked_server_unique_zone_section_assignment():
    clusters = [
        {
            "entity_ids": ["1", "2", "3", "4"],
            "texts": ["G2B", "END", "CEN", "INT"],
            "y_mean": 1000,
            "bbox": [0, 0, 1, 1],
        }
    ]
    label_pool = [
        {"id": "1", "x": 100.0, "y": 1200.0, "text": "G2B"},
        {"id": "2", "x": 200.0, "y": 900.0, "text": "END"},
        {"id": "3", "x": 600.0, "y": 900.0, "text": "CEN"},
        {"id": "4", "x": 1000.0, "y": 900.0, "text": "INT"},
    ]
    secs = [
        SectionCandidate(key="s0", x=210.0, y=600.0, bbox=(0, 0, 1, 1)),
        SectionCandidate(key="s1", x=610.0, y=600.0, bbox=(0, 0, 1, 1)),
        SectionCandidate(key="s2", x=1010.0, y=600.0, bbox=(0, 0, 1, 1)),
    ]
    out = compute_beam_horizontal_picked_server(clusters, label_pool, secs, row_y_tol=20)
    assert out is not None
    used_secs = set()
    used_zones = set()
    for L in out["zoneSectionLines"]:
        used_zones.add(str(L["from"]["eid"]))
        used_secs.add(str(L["to"]["key"]))
    assert len(used_zones) == len(used_secs)

