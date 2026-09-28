# HTTP agent services

A service is a sandbox with one managed server command. It uses the sandbox
lifetime and hourly meter. The server binds a loopback HTTP port inside
the sandbox. Nodus forwards authenticated requests over the runtime relay.
There is no inbound guest networking.

After authenticating, replace the image below with your qualified server image
and review its resource requirements:

```python
image = "your-account/inference:qualified"
with nodus.Client() as client:
    box = client.sandboxes.create(
        image=image,
        service={
            "command": ["python", "server.py"],
            "port": 8080,
            "health_path": "/health",
        },
        lifecycle={"max_lifetime_s": 90000, "idle_timeout_s": 1800},
        idempotency_key="inference-service-deployment-001",
    )
    print(box.id)
```

The image must already contain your server. Wait for the sandbox to become ready
before calling it. `box.request("POST", "/infer", port=8080, json=payload)` returns
response bytes. Use `json.loads(response)` for JSON or `response.decode("utf-8")`
for text. Send binary bodies with `content=payload_bytes` instead of `json=`.
Both synchronous and asynchronous service methods return bytes, including
`b""` for an empty response. The server
must return a successful 2xx response at its health path before an inference
request is forwarded.

The HTTPS API route is `/v1/sandboxes/{id}/ports/{port}/{path}` on your deployment
API origin. Send the normal Nodus bearer token. The route forwards the request
method, path, query, content type and body. Authorization and Cookie headers
never reach the guest. Other custom headers are not forwarded. Bodies are
limited to 64 KiB. WebSockets and streaming responses are not supported.
HTML responses run with an isolated sandbox origin.

HTTP calls have a 15 second relay deadline and are never automatically retried.
A timeout can mean the application received the request. Include an application
idempotency key in the JSON body before retrying a mutation. A suspended service
returns `service_waking` and starts recovery. That request was not sent to the
application. Retry after the sandbox becomes ready.

A stopped server restarts after a 30 second delay, up to 120 attempts per
runtime generation. Host recovery starts the server command again. Applications
must save and load their own state from `NODUS_CHECKPOINT_DIR`. This does not
restore process memory or make external tool calls exactly once.

Terminate a service with `box.terminate()` and verify its terminal state.
Read charges with `client.ledger(box.id)`. Passing local tests does not establish
24 hours of production availability.
