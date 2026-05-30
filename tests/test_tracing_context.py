"""Tests for context-local trace propagation."""

from concurrent.futures import ThreadPoolExecutor

from allstak.config import AllStakConfig
from allstak.modules.tracing import TracingModule
from allstak.transport import HttpTransport


def _module() -> TracingModule:
    transport = HttpTransport(api_key="ask_test", host="http://127.0.0.1:9")
    return TracingModule(transport, AllStakConfig(api_key="ask_test"))


def test_trace_context_is_thread_local():
    tracing = _module()

    def run(trace_id: str) -> tuple[str, str]:
        tracing.set_trace_id(trace_id)
        span = tracing.start_span("unit")
        current = tracing.get_current_span_id()
        span.finish()
        return tracing.get_trace_id(), current or ""

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(run, "a" * 32)
        b = pool.submit(run, "b" * 32)

    assert a.result()[0] == "a" * 32
    assert b.result()[0] == "b" * 32
    assert a.result()[1] != b.result()[1]


def test_finished_span_restores_current_span_stack():
    tracing = _module()
    tracing.set_trace_id("c" * 32)

    root = tracing.start_span("root")
    child = tracing.start_span("child")

    assert tracing.get_current_span_id() == child.span_id
    assert len(root.span_id) == 16
    assert len(child.span_id) == 16
    child.finish()
    assert tracing.get_current_span_id() == root.span_id
    root.finish()
    assert tracing.get_current_span_id() is None
