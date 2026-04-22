"""
Basic usage example — demonstrates core AllStak SDK features.

Run with:
    cd allstak-python
    pip install -e ".[dev]"
    python examples/basic_usage.py
"""

import os
import sys
import time

import allstak

# ---------------------------------------------------------------------------
# 1. Initialize the SDK
#
# Set ALLSTAK_API_KEY (and optionally ALLSTAK_HOST) in your environment
# before running this example. Never commit real API keys to source control.
# ---------------------------------------------------------------------------

api_key = os.environ.get("ALLSTAK_API_KEY")
if not api_key:
    sys.stderr.write("ALLSTAK_API_KEY is not set — aborting example.\n")
    sys.exit(1)

allstak.init(
    api_key=api_key,
    host=os.environ.get("ALLSTAK_HOST", "https://api.allstak.sa"),
    environment=os.environ.get("ALLSTAK_ENVIRONMENT", "development"),
    release=os.environ.get("ALLSTAK_RELEASE", "0.2.0"),
    debug=bool(os.environ.get("ALLSTAK_DEBUG")),
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
