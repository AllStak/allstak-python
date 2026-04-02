"""
Basic usage example — demonstrates core AllStak SDK features.

Run with:
    cd allstak-python
    pip install -e ".[dev]"
    python examples/basic_usage.py
"""

import sys
import time

import allstak

# ---------------------------------------------------------------------------
# 1. Initialize the SDK
# ---------------------------------------------------------------------------

allstak.init(
    api_key="ask_live_o5fmoedqr14vxm47rltn9frjpazjszh7",
    host="http://localhost:8080",
    environment="development",
    release="0.1.0",
    debug=True,
)
print("✓ AllStak SDK initialized")

# ---------------------------------------------------------------------------
# 2. Set user context
# ---------------------------------------------------------------------------

allstak.set_user(user_id="usr-demo-001", email="demo@allstak.dev")
print("✓ User context set")

# ---------------------------------------------------------------------------
# 3. Capture an exception
# ---------------------------------------------------------------------------

try:
    result = 1 / 0
except ZeroDivisionError as e:
    event_id = allstak.capture_exception(
        e,
        metadata={
            "source": "basic_usage_example",
            "operation": "demo_division",
        }
    )
    print(f"✓ Exception captured → event ID: {event_id}")

# ---------------------------------------------------------------------------
# 4. Capture an error by name (without exception object)
# ---------------------------------------------------------------------------

event_id = allstak.capture_error(
    exception_class="ExternalServiceError",
    message="Payment gateway timed out after 30s",
    level="error",
    metadata={
        "gateway": "stripe",
        "timeout_ms": 30000,
    }
)
print(f"✓ Error captured → event ID: {event_id}")

# ---------------------------------------------------------------------------
# 5. Log messages
# ---------------------------------------------------------------------------

allstak.log.info("Application started", service="demo-app")
allstak.log.warn(
    "Slow query detected",
    service="db-service",
    metadata={"query_ms": 4200, "table": "orders"},
)
allstak.log.error(
    "Failed to send email",
    service="notifications",
    metadata={"recipient": "user@example.com", "attempt": 3},
)
print("✓ Log messages queued")

# ---------------------------------------------------------------------------
# 6. Record HTTP requests
# ---------------------------------------------------------------------------

allstak.http.record(
    direction="outbound",
    method="POST",
    host="payments.stripe.com",
    path="/v1/charges",
    status_code=200,
    duration_ms=320,
    request_size=256,
    response_size=1024,
)

allstak.http.record(
    direction="inbound",
    method="GET",
    host="api.myapp.com",
    path="/api/users/me",
    status_code=200,
    duration_ms=18,
)
print("✓ HTTP requests recorded")

# ---------------------------------------------------------------------------
# 7. Session replay (server-side events)
# ---------------------------------------------------------------------------

import uuid
session_id = str(uuid.uuid4())

with allstak.replay.start_session(session_id=session_id) as session:
    session.record("navigation", {"from": "/home", "to": "/dashboard"})
    session.record("api_call", {"endpoint": "/api/users", "method": "GET"})
    print(f"✓ Replay session {session.session_id[:8]}... recorded")

# ---------------------------------------------------------------------------
# 8. Flush everything
# ---------------------------------------------------------------------------

print("Flushing all buffers...")
allstak.flush()
time.sleep(0.5)  # give background workers time to complete
print("✓ All events flushed")
print("\nDone! Check http://localhost:3000/overview for results.")
