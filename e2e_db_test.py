"""AllStak Python SDK -- DB Auto-Instrumentation E2E Test.

Uses manual database.record() to simulate real DB query capture,
since sqlite3.Cursor is a C extension and cannot be monkey-patched.
For real production use: psycopg2 and SQLAlchemy auto-instrumentation
would patch at the Python level.
"""

import os
import time
import sqlite3
import allstak

# Initialize
API_KEY = os.environ.get("ALLSTAK_API_KEY")
if not API_KEY:
    raise SystemExit("Set ALLSTAK_API_KEY to run the Python SDK DB E2E test.")

allstak.init(
    api_key=API_KEY,
    host="http://localhost:8080",
    environment="e2e-testing",
    debug=True,
)

print("=== AllStak Python SDK DB E2E Test ===\n")

# Use real sqlite3 for actual queries, but record via SDK
db = sqlite3.connect(":memory:")
cursor = db.cursor()


def timed_execute(sql, params=None):
    """Execute a query and record it via AllStak."""
    start = time.time()
    try:
        if params:
            result = cursor.execute(sql, params)
        else:
            result = cursor.execute(sql)
        duration = (time.time() - start) * 1000
        allstak.database.record(
            normalized_query=sql,
            duration_ms=duration,
            status="success",
            database_type="sqlite",
            database_name="memory",
            rows_affected=cursor.rowcount if cursor.rowcount >= 0 else -1,
        )
        return result
    except Exception as e:
        duration = (time.time() - start) * 1000
        allstak.database.record(
            normalized_query=sql,
            duration_ms=duration,
            status="error",
            error_message=str(e)[:500],
            database_type="sqlite",
            database_name="memory",
        )
        raise


# Schema creation
timed_execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, email TEXT)")
timed_execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER, total REAL)")
print("Tables created")

# Fast inserts (repeated pattern)
for i in range(5):
    timed_execute("INSERT INTO users VALUES (?, ?, ?)", (i, f"User {i}", f"user{i}@test.com"))
print("5 users inserted")

# Insert orders
for i in range(3):
    timed_execute("INSERT INTO orders VALUES (?, ?, ?)", (i, i, 19.99 + i * 10))
print("3 orders inserted")

# Repeated SELECT pattern
for i in range(4):
    timed_execute("SELECT * FROM users WHERE id = ?", (i,))
print("4 user lookups done")

# Slow-ish JOIN query (simulate with sleep)
time.sleep(0.5)
start = time.time()
cursor.execute("SELECT u.name, o.total FROM users u JOIN orders o ON u.id = o.user_id")
duration = (time.time() - start) * 1000 + 500  # Add the simulated delay
allstak.database.record(
    normalized_query="SELECT u.name, o.total FROM users u JOIN orders o ON u.id = o.user_id",
    duration_ms=duration,
    status="success",
    database_type="sqlite",
    database_name="memory",
    rows_affected=len(cursor.fetchall()),
)
print(f"JOIN query done ({duration:.0f}ms)")

# Aggregate query
timed_execute("SELECT COUNT(*), AVG(total) FROM orders")
print("Aggregate query done")

# Error query
try:
    timed_execute("SELECT * FROM nonexistent_table")
except Exception as e:
    print(f"Expected error captured: {e}")

# UPDATE query
timed_execute("UPDATE users SET email = ? WHERE id = ?", ("updated@test.com", 0))
print("Update done")

# DELETE query
timed_execute("DELETE FROM orders WHERE total < ?", (25.0,))
print("Delete done")

db.commit()
db.close()

print(f"\nTotal queries recorded. Waiting for flush...")
time.sleep(7)
allstak.shutdown()
print("Done!")
