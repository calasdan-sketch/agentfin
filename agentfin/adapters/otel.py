"""OpenTelemetry GenAI span adapter.

Reads spans written as JSON lines by an OTLP file exporter
(``{"resourceSpans": [...]}``, one export per line, ``intValue`` as strings) or
by the Python SDK's console/JSON exporter (``{"name", "context", "attributes",
"start_time", ...}``). Only token counts and identifiers are read; prompts,
completions and tool payloads (``gen_ai.input.messages`` etc.) are ignored.

Attribute names are those in the OpenTelemetry GenAI semantic conventions
(https://opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai/,
checked 2026-11-07): ``gen_ai.usage.input_tokens`` (SHOULD include cached
tokens), ``gen_ai.usage.cache_read.input_tokens``,
``gen_ai.usage.cache_creation.input_tokens``, ``gen_ai.usage.output_tokens``
(SHOULD include reasoning tokens), ``gen_ai.provider.name`` (older:
``gen_ai.system``), ``gen_ai.response.model`` / ``gen_ai.request.model``,
``gen_ai.response.id``.

Correctness rules:

* Cached tokens are split out of the input total, as the spec says the total
  includes them. If a library reports a total *smaller* than its cache buckets
  (some Anthropic instrumentations report the non-cached input only), the
  buckets are assumed exclusive, nothing is subtracted, and the event is marked
  ``otel_input_semantics: exclusive_assumed``.
* Agent/workflow spans (``invoke_agent``, ``invoke_workflow``, ...) and any span
  whose child spans also carry usage are skipped, because their usage is the sum
  of their children's. ``ingest`` reports how many were skipped.
* When a provider response id is present the event key is ``<provider>:<id>``,
  the same key the ``provider`` adapter writes, so a trace export and a raw API
  export of the same call do not double-count.
"""

from __future__ import annotations

from typing import Any

from .base import Adapter

_SKIP_OPERATIONS = {"invoke_agent", "invoke_workflow", "create_agent", "execute_tool",
                    "embeddings_batch"}
_PROVIDERS = {"gcp.gemini": "google", "gcp.vertex_ai": "google", "gemini": "google",
              "google": "google", "az.ai.openai": "openai", "azure.ai.openai": "openai",
              "aws.bedrock": "bedrock"}


def _val(v: Any) -> Any:
    """Decode one OTLP AnyValue (or pass a plain value through)."""
    if not isinstance(v, dict):
        return v
    for k in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if k in v:
            return v[k]
    return None


def _attrs(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return {str(k): _val(v) for k, v in raw.items()}
    out: dict[str, Any] = {}
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and "key" in item:
                out[str(item["key"])] = _val(item.get("value"))
    return out


def _int(v: Any) -> int:
    try:
        return max(int(float(v)), 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _ts(span: dict[str, Any]) -> Any:
    ns = span.get("startTimeUnixNano")
    if ns is not None:
        try:
            return int(ns) // 1_000_000_000
        except (TypeError, ValueError):
            return None
    return span.get("start_time") or span.get("startTime")


class OtelAdapter(Adapter):
    name = "otel"
    buffered = True

    def parse_record(self, d: dict[str, Any]) -> dict[str, Any] | None:
        events = self.parse_records(d)
        return events[0] if events else None

    def parse_records(self, d: dict[str, Any]) -> list[dict[str, Any]]:
        spans: list[tuple[dict[str, Any], dict[str, Any]]] = []
        if isinstance(d.get("resourceSpans"), list):
            for rs in d["resourceSpans"]:
                if not isinstance(rs, dict):
                    continue
                res = _attrs((rs.get("resource") or {}).get("attributes"))
                for ss in rs.get("scopeSpans") or rs.get("instrumentationLibrarySpans") or []:
                    for span in (ss or {}).get("spans") or []:
                        if isinstance(span, dict):
                            spans.append((span, res))
        elif "attributes" in d or "context" in d:
            res = _attrs((d.get("resource") or {}).get("attributes"))
            spans.append((d, res))
        out = []
        for span, res in spans:
            ev = self._span_event(span, res)
            if ev is not None:
                out.append(ev)
        return out

    def _span_event(self, span: dict[str, Any], res: dict[str, Any]) -> dict[str, Any] | None:
        a = _attrs(span.get("attributes"))
        has_usage = any(k.startswith("gen_ai.usage.") for k in a)
        if not has_usage:
            return None
        if str(a.get("gen_ai.operation.name") or "").lower() in _SKIP_OPERATIONS:
            self.skipped_aggregates = getattr(self, "skipped_aggregates", 0) + 1
            return None
        total_in = _int(a.get("gen_ai.usage.input_tokens", a.get("gen_ai.usage.prompt_tokens")))
        cache_read = _int(a.get("gen_ai.usage.cache_read.input_tokens"))
        cache_write = _int(a.get("gen_ai.usage.cache_creation.input_tokens"))
        out_tokens = _int(a.get("gen_ai.usage.output_tokens",
                                a.get("gen_ai.usage.completion_tokens")))
        meta: dict[str, Any] = {"source": "otel"}
        if total_in >= cache_read + cache_write:
            input_tokens = total_in - cache_read - cache_write
        else:
            input_tokens = total_in
            meta["otel_input_semantics"] = "exclusive_assumed"
        if not (input_tokens or cache_read or cache_write or out_tokens):
            return None
        if _int(a.get("gen_ai.usage.reasoning.output_tokens")):
            meta["reasoning_tokens"] = _int(a["gen_ai.usage.reasoning.output_tokens"])
        raw_provider = str(a.get("gen_ai.provider.name") or a.get("gen_ai.system")
                           or "unknown").lower()
        provider = _PROVIDERS.get(raw_provider, raw_provider)
        ctx = span.get("context") if isinstance(span.get("context"), dict) else {}
        trace_id = span.get("traceId") or ctx.get("trace_id")
        span_id = span.get("spanId") or ctx.get("span_id")
        parent = span.get("parentSpanId") or span.get("parent_id")
        meta.update({k: v for k, v in {
            "trace_id": trace_id, "span_id": span_id, "parent_span_id": parent or None,
            "response_id": a.get("gen_ai.response.id"),
            "service_tier": str(a.get("gen_ai.openai.response.service_tier") or "").lower()
            or None,
        }.items() if v})
        event: dict[str, Any] = {
            "ts": _ts(span),
            "provider": provider,
            "model_raw": str(a.get("gen_ai.response.model") or a.get("gen_ai.request.model")
                             or "unknown"),
            "input_tokens": input_tokens,
            "cache_read_tokens": cache_read,
            "cache_write_tokens": cache_write,
            "output_tokens": out_tokens,
            "task": str(a.get("agentfin.task") or a.get("gen_ai.workflow.name") or ""),
            "agent": str(a.get("agentfin.agent") or a.get("gen_ai.agent.name")
                         or res.get("service.name") or ""),
            "team": str(a.get("agentfin.team") or ""),
            "metadata": meta,
        }
        if meta.get("service_tier"):
            event["service_tier"] = meta["service_tier"]
        if a.get("gen_ai.response.id"):
            event["idem_key"] = f"{provider}:{a['gen_ai.response.id']}"
        elif trace_id and span_id:
            event["idem_key"] = f"otel:{trace_id}:{span_id}"
        return event

    def prefer(self, new: dict[str, Any], current: dict[str, Any]) -> bool:
        return new["output_tokens"] > current["output_tokens"]

    def post_reduce(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Drop a span whose child span also carries usage (it is their sum)."""
        parents = {e["metadata"].get("parent_span_id") for e in events} - {None}
        kept = [e for e in events if e["metadata"].get("span_id") not in parents]
        self.skipped_aggregates = getattr(self, "skipped_aggregates", 0) + (
            len(events) - len(kept))
        return kept
