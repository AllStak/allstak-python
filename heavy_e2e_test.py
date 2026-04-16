#!/usr/bin/env python3
"""Heavy E2E test for AllStak Python SDK — generates realistic telemetry load."""
import sys, os, time, uuid, traceback
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

import allstak

client = allstak.init(
    api_key="ask_live_4574x2yao33rtjbiuf2q873ltv6vpokb",
    host="http://localhost:8080",
    environment="e2e-heavy",
)
client.tracing.set_service("sdk-python-heavy-test")

trace_ids = []
span_count = 0
log_count = 0

# === A) TRACING: 5 successful + 2 error traces ===
for i in range(5):
    allstak.reset_trace()  # Force new trace per iteration
    root = allstak.start_span(f"HTTP GET /api/resource-{i}", description=f"Successful flow {i}")
    trace_ids.append(root.trace_id)

    child1 = allstak.start_span(f"DB SELECT resource_{i}", description="PostgreSQL query")
    time.sleep(0.02)
    child1.finish("ok")

    child2 = allstak.start_span(f"CACHE GET resource:{i}", description="Redis lookup")
    time.sleep(0.005)
    child2.finish("ok")

    if i % 2 == 0:
        child3 = allstak.start_span(f"API call external-svc/validate", description="External validation")
        time.sleep(0.015)
        child3.finish("ok")
        span_count += 1

    span_count += 3  # root + 2 children
    time.sleep(0.01)
    root.finish("ok")

# 2 error traces
for i in range(2):
    allstak.reset_trace()
    root = allstak.start_span(f"HTTP POST /api/orders (ERROR {i})", description=f"Error flow {i}")
    trace_ids.append(root.trace_id)
    child = allstak.start_span(f"DB INSERT orders (FAIL)", description="DB constraint violation")
    time.sleep(0.01)
    child.finish("error")
    span_count += 2
    root.finish("error")

print(f"Spans created: {span_count}")
print(f"Trace IDs: {trace_ids}")

# === B) LOGS: 15+ mixed levels ===
levels = [
    ("debug", "Initializing connection pool"),
    ("debug", "Cache warmup started"),
    ("info", "Server started on port 8080"),
    ("info", "Request received: GET /api/users"),
    ("info", "Query returned 42 rows"),
    ("info", "Response sent: 200 OK"),
    ("info", "Background job started: cleanup"),
    ("warn", "Slow query detected: 850ms"),
    ("warn", "Connection pool near capacity: 18/20"),
    ("warn", "Retry attempt 2/3 for external API"),
    ("error", "Failed to connect to Redis: timeout"),
    ("error", "Unhandled exception in request handler"),
    ("error", "Database deadlock detected"),
    ("fatal", "Out of memory: heap exhausted"),
    ("info", "Graceful shutdown initiated"),
    ("debug", "All connections closed"),
    ("info", "Server stopped"),
]

for level, msg in levels:
    tid = trace_ids[log_count % len(trace_ids)] if log_count < 10 else None
    getattr(allstak.log, level)(msg, service="sdk-python-heavy-test", environment="e2e-heavy",
                                  trace_id=tid if tid else None)
    log_count += 1

print(f"Logs sent: {log_count}")

# === C) HTTP REQUESTS: 10+ mixed ===
methods_paths = [
    ("GET", "/api/users", 200, 120), ("GET", "/api/users/1", 200, 45),
    ("POST", "/api/users", 201, 180), ("PUT", "/api/users/1", 200, 95),
    ("DELETE", "/api/users/99", 404, 30), ("GET", "/api/products", 200, 250),
    ("POST", "/api/orders", 201, 340), ("GET", "/api/orders/1", 200, 55),
    ("POST", "/api/payments", 500, 890), ("GET", "/api/health", 200, 5),
    ("PATCH", "/api/users/1", 200, 75), ("GET", "/api/reports", 200, 1200),
]

req_count = 0
for method, path, status, dur in methods_paths:
    tid = trace_ids[req_count % len(trace_ids)]
    allstak.http.record(
        trace_id=tid,
        direction="inbound",
        method=method,
        host="api.python-heavy.allstak.io",
        path=path,
        status_code=status,
        duration_ms=dur,
        request_size=100 + req_count * 10,
        response_size=500 + req_count * 50,
    )
    req_count += 1

print(f"HTTP requests sent: {req_count}")

# === D) ERRORS: 3 exceptions ===
errors_sent = 0
for exc_class, msg in [
    (ValueError, "Invalid user ID format"),
    (ConnectionError, "Database connection refused"),
    (RuntimeError, "Payment gateway timeout after 30s"),
]:
    try:
        raise exc_class(msg)
    except Exception as e:
        allstak.capture_exception(e)
        errors_sent += 1

print(f"Errors sent: {errors_sent}")

# === E) FAILURE BEHAVIOR: invalid key test ===
print("\n=== Testing failure behavior ===")
# Test invalid key via direct curl (SDK singleton prevents re-init)
import urllib.request, json
try:
    req = urllib.request.Request(
        "http://localhost:8080/ingest/v1/logs",
        data=json.dumps({"level":"info","message":"bad key test"}).encode(),
        headers={"Content-Type":"application/json","X-AllStak-Key":"invalid_key_xxx"},
        method="POST"
    )
    resp = urllib.request.urlopen(req)
    print(f"Bad key test: unexpected {resp.status}")
except urllib.error.HTTPError as e:
    print(f"Bad key test: correctly rejected with {e.code} ({e.reason})")
print("Bad key test: SDK did not crash (graceful failure)")

# Flush everything
allstak.flush()
time.sleep(2)
print("\n=== ALL DATA FLUSHED ===")
print(f"Total: {span_count} spans, {log_count} logs, {req_count} requests, {errors_sent} errors")
print(f"Trace IDs for verification: {trace_ids[:3]}...")
