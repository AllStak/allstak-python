"""
End-to-end test for AllStak Python SDK against localhost:8080.

Tests: tracing (spans), logs, HTTP requests, error capture.
"""

import time
import allstak

# ---------------------------------------------------------------------------
# 1. Initialize the SDK
# ---------------------------------------------------------------------------

SERVICE = "sdk-python-test"
ENVIRONMENT = "e2e-testing"

client = allstak.init(
    api_key="ask_live_4574x2yao33rtjbiuf2q873ltv6vpokb",
    host="http://localhost:8080",
    environment=ENVIRONMENT,
    debug=True,
    max_retries=1,
    flush_interval_ms=500,
    connect_timeout=5.0,
    read_timeout=5.0,
)

# Set service name on the tracing module
client.tracing.set_service(SERVICE)

print(f"[OK] SDK initialized (host=http://localhost:8080, service={SERVICE})")

# ---------------------------------------------------------------------------
# 2. Distributed Tracing - root + child span
# ---------------------------------------------------------------------------

# Start root span
root_span = allstak.start_span(
    "HTTP GET /api/products",
    description="Fetch product catalog",
    tags={"http.method": "GET", "http.url": "/api/products"},
)
trace_id = root_span.trace_id
print(f"[OK] Root span started: trace_id={trace_id}, span_id={root_span.span_id}")

# Simulate some work
time.sleep(0.05)

# Start child span
child_span = allstak.start_span(
    "DB SELECT products",
    description="SELECT * FROM products WHERE active=true",
    tags={"db.type": "postgresql", "db.statement": "SELECT"},
)
print(f"[OK] Child span started: span_id={child_span.span_id}, parent={child_span.parent_span_id}")

# Simulate DB query
time.sleep(0.03)

# Finish child span
child_span.finish("ok")
print("[OK] Child span finished")

# Finish root span
time.sleep(0.02)
root_span.finish("ok")
print("[OK] Root span finished")

# ---------------------------------------------------------------------------
# 3. Logs with trace_id
# ---------------------------------------------------------------------------

allstak.log.info(
    "Product catalog fetched successfully",
    service=SERVICE,
    trace_id=trace_id,
    environment=ENVIRONMENT,
    metadata={"product_count": 42},
)

allstak.log.warn(
    "Slow query detected in products table",
    service=SERVICE,
    trace_id=trace_id,
    environment=ENVIRONMENT,
    metadata={"query_ms": 3200, "table": "products"},
)

allstak.log.error(
    "Cache miss for product catalog",
    service=SERVICE,
    trace_id=trace_id,
    environment=ENVIRONMENT,
    metadata={"cache_key": "products:active:v2"},
)

print(f"[OK] 3 log messages queued (trace_id={trace_id})")

# ---------------------------------------------------------------------------
# 4. HTTP Request Records (3 inbound requests, varying methods/statuses)
# ---------------------------------------------------------------------------

allstak.http.record(
    direction="inbound",
    method="GET",
    host="api.myapp.com",
    path="/api/products",
    status_code=200,
    duration_ms=85,
    request_size=0,
    response_size=4096,
    trace_id=trace_id,
)

allstak.http.record(
    direction="inbound",
    method="POST",
    host="api.myapp.com",
    path="/api/orders",
    status_code=201,
    duration_ms=120,
    request_size=512,
    response_size=256,
    trace_id=trace_id,
)

allstak.http.record(
    direction="inbound",
    method="DELETE",
    host="api.myapp.com",
    path="/api/products/99",
    status_code=404,
    duration_ms=12,
    request_size=0,
    response_size=64,
    trace_id=trace_id,
)

allstak.http.record(
    direction="outbound",
    method="GET",
    host="cache.redis.internal",
    path="/products/catalog",
    status_code=200,
    duration_ms=3,
    trace_id=trace_id,
)

print("[OK] 4 HTTP request records queued")

# ---------------------------------------------------------------------------
# 5. Error / Exception capture with trace context
# ---------------------------------------------------------------------------

try:
    # Simulate a real exception
    data = {"products": None}
    count = len(data["products"]["items"])  # TypeError
except Exception as e:
    event_id = allstak.capture_exception(
        e,
        level="error",
        environment=ENVIRONMENT,
        metadata={
            "source": "e2e_test",
            "operation": "product_count",
            "traceId": trace_id,
        },
    )
    print(f"[OK] Exception captured -> event_id={event_id}")

# ---------------------------------------------------------------------------
# 6. Flush all data
# ---------------------------------------------------------------------------

print("\nFlushing all buffers...")
allstak.flush()
time.sleep(1)  # give background workers time to complete
allstak.flush()  # second flush to catch stragglers
time.sleep(0.5)

print("\n" + "=" * 60)
print("E2E TEST COMPLETE")
print("=" * 60)
print(f"  Trace ID:    {trace_id}")
print(f"  Service:     {SERVICE}")
print(f"  Environment: {ENVIRONMENT}")
print(f"  Spans sent:  2 (root + child)")
print(f"  Logs sent:   3 (info, warn, error)")
print(f"  HTTP reqs:   4 (3 inbound + 1 outbound)")
print(f"  Errors:      1 (TypeError)")
print("=" * 60)
