import json
import logging
import os
import time
import urllib.request
import uuid

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Captured once at module import. With SnapStart, this UUID is baked into the
# snapshot — every restored container will report the SAME init_uuid, which is
# how you can visually confirm snapshot sharing across invocations.
INIT_UUID = str(uuid.uuid4())
INIT_TIME = time.time()
INIT_TYPE = os.environ.get("AWS_LAMBDA_INITIALIZATION_TYPE", "unknown")
INVOCATION_COUNT = 0

logger.info(
    "module-init init_uuid=%s init_time=%.3f init_type=%s pid=%d",
    INIT_UUID,
    INIT_TIME,
    INIT_TYPE,
    os.getpid(),
)


def lambda_handler(event, context):
    global INVOCATION_COUNT
    INVOCATION_COUNT += 1

    url = os.environ.get("EXTERNAL_URL", "https://httpbin.org/get")
    request_id = getattr(context, "aws_request_id", "n/a")
    # If this differs from INIT_TYPE, the env was rewritten after restore
    # (snap-start invocations should report snap-start here too).
    runtime_init_type = os.environ.get("AWS_LAMBDA_INITIALIZATION_TYPE", "unknown")

    logger.info(
        "invoke-start request_id=%s invocation=%d init_uuid=%s "
        "init_type=%s runtime_init_type=%s age_since_init_sec=%.2f",
        request_id,
        INVOCATION_COUNT,
        INIT_UUID,
        INIT_TYPE,
        runtime_init_type,
        time.time() - INIT_TIME,
    )

    started = time.perf_counter()
    http_request = urllib.request.Request(
        url, headers={"User-Agent": "lambda-snap-start-test/1.0"}
    )
    with urllib.request.urlopen(http_request, timeout=10) as response:
        status = response.status
        body = response.read().decode("utf-8")
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)

    logger.info(
        "external-call request_id=%s url=%s status=%d elapsed_ms=%.2f",
        request_id,
        url,
        status,
        elapsed_ms,
    )

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(
            {
                "init_uuid": INIT_UUID,
                "init_type": INIT_TYPE,
                "runtime_init_type": runtime_init_type,
                "invocation_count_in_container": INVOCATION_COUNT,
                "age_since_init_sec": round(time.time() - INIT_TIME, 2),
                "external_url": url,
                "external_status": status,
                "elapsed_ms": elapsed_ms,
                "external_body_preview": body[:500],
            }
        ),
    }
