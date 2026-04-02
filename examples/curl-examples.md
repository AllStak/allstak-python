# AllStak API — curl Examples

Real API key and backend URL used during SDK development and validation.

```bash
API_KEY="ask_live_o5fmoedqr14vxm47rltn9frjpazjszh7"
BASE="http://localhost:8080"
```

---

## Authentication

### Valid API Key
```bash
curl -s -w "\nHTTP:%{http_code}" -X POST "$BASE/ingest/v1/logs" \
  -H "Content-Type: application/json" \
  -H "X-AllStak-Key: $API_KEY" \
  -d '{"level":"info","message":"curl auth test"}'

# Expected: HTTP:202
# Response: {"success":true,"data":{"id":"<uuid>"},...}
```

### Invalid API Key → 401
```bash
curl -s -w "\nHTTP:%{http_code}" -X POST "$BASE/ingest/v1/errors" \
  -H "Content-Type: application/json" \
  -H "X-AllStak-Key: bad-key-value" \
  -d '{"exceptionClass":"Test","message":"test"}'

# Expected: HTTP:401
# Response: {"error":{"message":"Invalid or missing API key","code":"INVALID_API_KEY"},"success":false}
```

### Missing Required Field → 422
```bash
curl -s -w "\nHTTP:%{http_code}" -X POST "$BASE/ingest/v1/errors" \
  -H "Content-Type: application/json" \
  -H "X-AllStak-Key: $API_KEY" \
  -d '{"message":"missing exceptionClass"}'

# Expected: HTTP:422 (NOTE: guidelines say 400 — actual backend returns 422)
# Response: {"success":false,"error":{"code":"VALIDATION_ERROR","message":"Validation failed","details":{"exceptionClass":"must not be blank"}}}
```

---

## Error Ingestion — POST /ingest/v1/errors

### Minimal error
```bash
curl -s -w "\nHTTP:%{http_code}" -X POST "$BASE/ingest/v1/errors" \
  -H "Content-Type: application/json" \
  -H "X-AllStak-Key: $API_KEY" \
  -d '{
    "exceptionClass": "ValueError",
    "message": "Invalid input value"
  }'

# Expected: HTTP:202
```

### Full error with all fields
```bash
curl -s -w "\nHTTP:%{http_code}" -X POST "$BASE/ingest/v1/errors" \
  -H "Content-Type: application/json" \
  -H "X-AllStak-Key: $API_KEY" \
  -d '{
    "exceptionClass": "DatabaseConnectionError",
    "message": "Connection refused to postgres:5432",
    "stackTrace": [
      "File db.py, line 42, in connect",
      "  raise DatabaseConnectionError(msg)"
    ],
    "level": "fatal",
    "environment": "production",
    "release": "v2.3.1",
    "sessionId": "sess-demo-001",
    "user": {
      "id": "usr-001",
      "email": "user@example.com",
      "ip": "192.168.1.1"
    },
    "metadata": {
      "host": "db.internal",
      "port": 5432
    }
  }'

# Expected: HTTP:202
```

### Bad level value → 422
```bash
curl -s -w "\nHTTP:%{http_code}" -X POST "$BASE/ingest/v1/errors" \
  -H "Content-Type: application/json" \
  -H "X-AllStak-Key: $API_KEY" \
  -d '{"exceptionClass":"E","message":"m","level":"trace"}'

# Expected: HTTP:422 — "trace" is not a valid level
```

---

## Log Ingestion — POST /ingest/v1/logs

### Info log
```bash
curl -s -w "\nHTTP:%{http_code}" -X POST "$BASE/ingest/v1/logs" \
  -H "Content-Type: application/json" \
  -H "X-AllStak-Key: $API_KEY" \
  -d '{
    "level": "info",
    "message": "Order placed successfully",
    "service": "order-service",
    "traceId": "trace-abc-001",
    "metadata": {
      "orderId": "ORD-5512",
      "amount": 99.90
    }
  }'

# Expected: HTTP:202
```

### Warning — use "warn" NOT "warning"
```bash
curl -s -w "\nHTTP:%{http_code}" -X POST "$BASE/ingest/v1/logs" \
  -H "Content-Type: application/json" \
  -H "X-AllStak-Key: $API_KEY" \
  -d '{"level":"warn","message":"Slow query detected"}'

# Expected: HTTP:202

# "warning" is INVALID for logs (valid for errors only):
curl -s -w "\nHTTP:%{http_code}" -X POST "$BASE/ingest/v1/logs" \
  -H "Content-Type: application/json" \
  -H "X-AllStak-Key: $API_KEY" \
  -d '{"level":"warning","message":"this will fail"}'

# Expected: HTTP:422
```

---

## HTTP Request Monitoring — POST /ingest/v1/http-requests

### Single outbound request
```bash
curl -s -w "\nHTTP:%{http_code}" -X POST "$BASE/ingest/v1/http-requests" \
  -H "Content-Type: application/json" \
  -H "X-AllStak-Key: $API_KEY" \
  -d '{
    "requests": [
      {
        "traceId": "trace-py-001",
        "direction": "outbound",
        "method": "POST",
        "host": "payments.stripe.com",
        "path": "/v1/charges",
        "statusCode": 200,
        "durationMs": 320,
        "requestSize": 256,
        "responseSize": 1024,
        "timestamp": "2026-03-31T20:00:00Z"
      }
    ]
  }'

# Expected: HTTP:202
# Response: {"ok":true,"accepted":1}
# NOTE: HTTP requests endpoint uses {"ok":true} not {"success":true}
```

### Batch of 2 requests
```bash
curl -s -w "\nHTTP:%{http_code}" -X POST "$BASE/ingest/v1/http-requests" \
  -H "Content-Type: application/json" \
  -H "X-AllStak-Key: $API_KEY" \
  -d '{
    "requests": [
      {
        "traceId": "trace-001",
        "direction": "outbound",
        "method": "GET",
        "host": "api.example.com",
        "path": "/v1/data",
        "statusCode": 200,
        "durationMs": 142,
        "requestSize": 0,
        "responseSize": 512,
        "timestamp": "2026-03-31T12:00:00Z"
      },
      {
        "traceId": "trace-002",
        "direction": "inbound",
        "method": "POST",
        "host": "myapp.com",
        "path": "/api/orders",
        "statusCode": 201,
        "durationMs": 55,
        "requestSize": 1024,
        "responseSize": 256,
        "timestamp": "2026-03-31T12:00:01Z"
      }
    ]
  }'

# Expected: HTTP:202, {"ok":true,"accepted":2}
```

---

## Session Replay — POST /ingest/v1/replay

### Navigation event
```bash
curl -s -w "\nHTTP:%{http_code}" -X POST "$BASE/ingest/v1/replay" \
  -H "Content-Type: application/json" \
  -H "X-AllStak-Key: $API_KEY" \
  -d '{
    "fingerprint": "sess-curl-test-001",
    "sessionId": "sess-curl-test-001",
    "events": [
      {
        "eventType": "navigation",
        "eventData": "{\"from\":\"/home\",\"to\":\"/checkout\"}",
        "url": "http://localhost:3000/checkout",
        "timestampMillis": 1711886400000
      }
    ]
  }'

# Expected: HTTP:202
# Response: {"success":true,"data":{"eventsReceived":1},...}
```

### Multiple events
```bash
curl -s -w "\nHTTP:%{http_code}" -X POST "$BASE/ingest/v1/replay" \
  -H "Content-Type: application/json" \
  -H "X-AllStak-Key: $API_KEY" \
  -d '{
    "fingerprint": "sess-multi-001",
    "sessionId": "sess-multi-001",
    "events": [
      {
        "eventType": "navigation",
        "eventData": "{\"url\":\"/checkout\"}",
        "timestampMillis": 1711886400000
      },
      {
        "eventType": "mouse_click",
        "eventData": "{\"x\":512,\"y\":340,\"target\":\"button#place-order\"}",
        "timestampMillis": 1711886405123
      },
      {
        "eventType": "input",
        "eventData": "{\"field\":\"card-number\",\"value\":\"[MASKED]\"}",
        "timestampMillis": 1711886407800
      }
    ]
  }'

# Expected: HTTP:202, {"data":{"eventsReceived":3}}
```

---

## Cron Heartbeat — POST /ingest/v1/heartbeat

### Unknown slug → 404
```bash
curl -s -w "\nHTTP:%{http_code}" -X POST "$BASE/ingest/v1/heartbeat" \
  -H "Content-Type: application/json" \
  -H "X-AllStak-Key: $API_KEY" \
  -d '{
    "slug": "nonexistent-job",
    "status": "success",
    "durationMs": 1500
  }'

# Expected: HTTP:404
# Response: {"code":"NOT_FOUND","ok":false,"message":"No cron monitor found with slug: nonexistent-job"}
# NOTE: Slug must match a CronMonitor created in the AllStak console
```

### Success ping (with registered slug)
```bash
# First create a CronMonitor via the management console with slug "daily-report"
# Then:
curl -s -w "\nHTTP:%{http_code}" -X POST "$BASE/ingest/v1/heartbeat" \
  -H "Content-Type: application/json" \
  -H "X-AllStak-Key: $API_KEY" \
  -d '{
    "slug": "daily-report",
    "status": "success",
    "durationMs": 4320,
    "message": "Processed 1842 records"
  }'

# Expected: HTTP:202, {"ok":true,"monitorId":"<uuid>"}
```

### Failed ping — triggers alerts
```bash
curl -s -w "\nHTTP:%{http_code}" -X POST "$BASE/ingest/v1/heartbeat" \
  -H "Content-Type: application/json" \
  -H "X-AllStak-Key: $API_KEY" \
  -d '{
    "slug": "daily-report",
    "status": "failed",
    "durationMs": 120,
    "message": "Database connection refused"
  }'

# Expected: HTTP:202, alert triggered server-side
```

---

## Health Check

```bash
curl -s http://localhost:8080/actuator/health
# Expected: {"status":"UP"}
```

---

## Actual Test Results

All commands above were validated against `http://localhost:8080` on 2026-03-31.

| Endpoint | Test | Result |
|---|---|---|
| `/ingest/v1/errors` | Valid payload | 202 ✓ |
| `/ingest/v1/errors` | Invalid API key | 401 ✓ |
| `/ingest/v1/errors` | Missing field | 422 ✓ (not 400 as guidelines say) |
| `/ingest/v1/logs` | Valid payload | 202 ✓ |
| `/ingest/v1/logs` | "warning" level | 422 ✓ |
| `/ingest/v1/http-requests` | Single item | 202 ✓ |
| `/ingest/v1/http-requests` | Batch of 2 | 202 ✓ |
| `/ingest/v1/replay` | Single event | 202 ✓ |
| `/ingest/v1/replay` | Multiple events | 202 ✓ |
| `/ingest/v1/heartbeat` | Unknown slug | 404 ✓ |
| `/actuator/health` | Health check | 200 ✓ |
