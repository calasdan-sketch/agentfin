import json

from agentfin.adapters.gateway import GatewayAdapter
from agentfin.adapters.jsonl import JsonlAdapter
from agentfin.adapters.provider import ProviderAdapter


def test_jsonl_basic():
    line = json.dumps({
        "ts": "2026-10-19T09:00:00Z", "provider": "anthropic",
        "model": "claude-sonnet-4-5", "input_tokens": 100, "output_tokens": 50,
        "agent": "cc", "team": "platform", "task": "review",
        "message_id": "msg_1", "metadata": {"run": "r1"},
    })
    events = list(JsonlAdapter().parse([line]))
    assert len(events) == 1
    e = events[0]
    assert e["idem_key"] == "anthropic:msg_1"
    assert (e["input_tokens"], e["output_tokens"]) == (100, 50)
    assert e["metadata"]["run"] == "r1"


def test_jsonl_nested_usage_and_epoch():
    line = json.dumps({
        "created_at": 1760878920, "provider": "openai", "model": "gpt-4o-mini",
        "usage": {"input": 10, "output": 5},
    })
    e = list(JsonlAdapter().parse([line]))[0]
    assert (e["input_tokens"], e["output_tokens"]) == (10, 5)
    assert "idem_key" not in e  # content-hash fallback assigned at emit


def test_provider_anthropic_shape():
    line = json.dumps({
        "id": "msg_01Ab", "model": "claude-sonnet-4-5", "created": 1760878920,
        "usage": {"input_tokens": 100, "cache_creation_input_tokens": 40,
                  "cache_read_input_tokens": 60, "output_tokens": 50},
        "agentfin": {"agent": "jarvis", "team": "platform", "task": "triage"},
    })
    e = list(ProviderAdapter().parse([line]))[0]
    assert e["provider"] == "anthropic"
    assert e["idem_key"] == "anthropic:msg_01Ab"
    assert e["cache_read_tokens"] == 60
    assert e["cache_write_tokens"] == 40
    assert e["agent"] == "jarvis"


def test_provider_openai_shape():
    line = json.dumps({
        "id": "chatcmpl-xyz", "model": "gpt-4o-mini", "created": 1760878920,
        "usage": {"prompt_tokens": 200, "completion_tokens": 30,
                  "prompt_tokens_details": {"cached_tokens": 50}},
    })
    e = list(ProviderAdapter().parse([line]))[0]
    assert e["provider"] == "openai"
    # cached_tokens is a subset of prompt_tokens: 150 uncached + 50 cached.
    assert (e["input_tokens"], e["cache_read_tokens"]) == (150, 50)


def test_gateway_shape_and_precomputed_cost():
    line = json.dumps({
        "ts": "2026-10-19T09:00:00Z", "route": "anthropic/claude-sonnet-4-5",
        "tokens_in": 100, "tokens_out": 50, "tokens_cached": 20,
        "agent": "jarvis", "project": "onfile", "run_id": "run_1",
        "cost_usd": 0.0042,
    })
    e = list(GatewayAdapter().parse([line]))[0]
    assert e["provider"] == "anthropic"
    assert e["model_raw"] == "claude-sonnet-4-5"
    assert e["task"] == "onfile"
    assert e["_gateway_cost_micro"] == 4200
    assert e["idem_key"] == "gateway:run_1"


def test_blank_and_comment_lines_skipped():
    events = list(JsonlAdapter().parse(["", "# comment", "   "]))
    assert events == []
