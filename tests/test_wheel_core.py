from wheel_core import WheelScenario, parse_option_symbol, snap_otm_grid


def test_option_symbol_parser():
    parsed = parse_option_symbol("AAPL260918C00230000")
    assert parsed == {"underlying": "AAPL", "expiration": "2026-09-18", "type": "call", "strike": 230.0}


def test_snapped_otm_grid_stays_otm():
    strikes = [200, 202.5, 205, 207.5, 210, 212.5, 215, 217.5, 220, 222.5, 225]
    assert snap_otm_grid(strikes, 216.2, "call", 3) == [220, 222.5, 225]
    assert snap_otm_grid(strikes, 216.2, "put", 3) == [215, 212.5, 210]


def test_scenario_matches_source_formulas():
    result = WheelScenario(200, 216.83, 215.51, 222.5, 2.07, 210, 1.45).as_dict()
    assert result["stock_pl"] == -264.0
    assert result["call_premium"] == 414.0
    assert result["called_away_pl"] == 1134.0
    assert result["put_cash_required"] == 42000.0
    assert result["post_assignment_basis"] == 213.41
