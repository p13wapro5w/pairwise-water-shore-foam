import pytest
import render.water_shore_foam as mod

CASES = [
    ("tide_update", (0.5, 1.0)),
    ("uv_wave_dir", (0.2, 0.4)),
    ("wave_height_drive", (1.0, 2.0)),
    ("edge_fade", (0.5, 0.8)),
    ("depth_write_off", (1, 2)),
    ("camera_consistent", (1, 2)),
    ("surface_fit", (1, 2)),
    ("night_atten", (0.5, 0.2)),
    ("distance_lod", (100.0, 20.0)),
    ("intersect_fix", (1, 2)),
    ("foam_mask", (0.3, 0.7)),
    ("foam_density", (0.5, 0.2)),
    ("shore_depth", (2.0, 0.5)),
    ("flow_speed", (1.0, 2.0)),
    ("reflect_order", (1, 2)),
]

@pytest.mark.parametrize("fn_name,args", CASES)
def test_contract(fn_name, args):
    fn = getattr(mod, fn_name)
    assert fn(*args) is not None
