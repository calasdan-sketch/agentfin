from agentfin import attribution, pricing


def test_cost_math_micro_usd():
    rates = {"openai:gpt-4o": {"input": 2.50, "output": 10.00}}
    event = {"input_tokens": 1_000_000, "output_tokens": 500_000}
    # 1M in @ $2.50 = $2.50 ; 0.5M out @ $10 = $5.00 ; total $7.50 = 7_500_000 micro
    assert pricing.cost_micro_usd("openai:gpt-4o", rates, event) == 7_500_000


def test_unpriced_sku_returns_none():
    assert pricing.cost_micro_usd("nope:unknown-model", {}, {}) is None


def test_cache_tokens_priced_separately():
    rates = {
        "anthropic:claude-sonnet-4-5": {
            "input": 3.0,
            "cache_read": 0.30,
            "cache_write": 3.75,
            "output": 15.0,
        }
    }
    event = {"input_tokens": 0, "cache_read_tokens": 1_000_000,
             "cache_write_tokens": 0, "output_tokens": 0}
    assert pricing.cost_micro_usd("anthropic:claude-sonnet-4-5", rates, event) == 300_000


def test_format_usd():
    assert pricing.format_usd(1_234_567) == "$1.23"
    assert pricing.format_usd(None) == "UNPRICED"


def test_attribution_first_match_wins():
    cfg = {
        "defaults": {"task": "unattributed", "agent": "unknown", "team": "unassigned"},
        "rules": [
            {"match": {"model": ".*sonnet.*"}, "set": {"agent": "claude-code"}},
            {"match": {"model": ".*"}, "set": {"agent": "fallback"}},
        ],
    }
    event = {"provider": "anthropic", "model_raw": "claude-sonnet-4-5",
             "metadata": {}}
    assert attribution.apply_rules(event, cfg) == (
        "unattributed", "claude-code", "unassigned")


def test_adapter_tags_win_over_rules():
    cfg = {
        "defaults": {},
        "rules": [{"match": {"model": ".*"}, "set": {"agent": "from-rule"}}],
    }
    event = {"provider": "x", "model_raw": "m", "agent": "from-adapter",
             "metadata": {}}
    _, agent, _ = attribution.apply_rules(event, cfg)
    assert agent == "from-adapter"


def test_meta_condition():
    cfg = {
        "defaults": {},
        "rules": [{"match": {"meta.task": "support.*"},
                   "set": {"team": "support", "task": "support-triage"}}],
    }
    event = {"provider": "x", "model_raw": "m",
             "metadata": {"task": "support-inbox"}}
    task, _, team = attribution.apply_rules(event, cfg)
    assert (task, team) == ("support-triage", "support")
