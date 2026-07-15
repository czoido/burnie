from burnie.pricing import calc_cost, calc_cost_components, get_context_window, get_model_pricing


def test_get_model_pricing_matches_prefix():
    p = get_model_pricing('claude-sonnet-4-6-20260101')
    assert p['input'] == 3.00
    assert p['output'] == 15.00


def test_get_model_pricing_unknown_model_falls_back_to_default():
    p = get_model_pricing('some-future-model')
    assert p == {'input': 3.00, 'output': 15.00, 'cacheWrite5m': 3.75, 'cacheWrite1h': 6.00, 'cacheRead': 0.30}


def test_get_model_pricing_no_model_falls_back_to_default():
    assert get_model_pricing(None)['input'] == 3.00


def test_sonnet_5_price_changes_after_introductory_period():
    before = get_model_pricing('claude-sonnet-5', date='2026-08-31')
    after = get_model_pricing('claude-sonnet-5', date='2026-09-01')
    assert before['input'] == 2.00
    assert after['input'] == 3.00


def test_calc_cost_components_basic_input_output():
    usage = {'input_tokens': 1_000_000, 'output_tokens': 1_000_000}
    c = calc_cost_components(usage, 'claude-sonnet-4-6')
    assert c['input'] == 3.00
    assert c['output'] == 15.00
    assert c['cacheWrite'] == 0
    assert c['cacheRead'] == 0


def test_calc_cost_components_cache_read_cheaper_than_input():
    usage = {'input_tokens': 0, 'output_tokens': 0, 'cache_read_input_tokens': 1_000_000}
    c = calc_cost_components(usage, 'claude-sonnet-4-6')
    assert c['cacheRead'] == 0.30
    # cacheSavings is the counterfactual: what those tokens would have cost as fresh input, minus
    # what they actually cost.
    assert round(c['cacheSavings'], 2) == round(3.00 - 0.30, 2)


def test_calc_cost_sums_all_components():
    usage = {'input_tokens': 1_000_000, 'output_tokens': 1_000_000, 'cache_read_input_tokens': 1_000_000}
    total = calc_cost(usage, 'claude-sonnet-4-6')
    components = calc_cost_components(usage, 'claude-sonnet-4-6')
    assert total == components['input'] + components['output'] + components['cacheWrite'] + components['cacheRead']


def test_get_context_window_known_and_unknown_models():
    assert get_context_window('claude-opus-4-8') == 1_000_000
    assert get_context_window('claude-haiku-3-5') == 200_000
    assert get_context_window('totally-unknown-model') == 200_000
    assert get_context_window(None) == 200_000
