"""The script a client container runs to call the dashboard over HTTPS, and its Python."""

PYTHON = "/opt/homeproxy/venv/bin/python"


# Runs inside a client container. It sends each request on a new TLS connection and prints one
# JSON result per request: the status, headers and body, or the name of the error when no HTTP
# response arrived. Certificate checks are off here; the shared fixtures cover them.
CLIENT_SCRIPT = r"""
import http.client, json, ssl, sys
host, port, steps = sys.argv[1], int(sys.argv[2]), json.loads(sys.argv[3])
context = ssl.create_default_context()
context.check_hostname = False
context.verify_mode = ssl.CERT_NONE
results = []
for step in steps:
    connection = http.client.HTTPSConnection(host, port, context=context, timeout=8)
    try:
        headers = dict(step.get("headers", {}))
        body = None
        if step.get("body") is not None:
            body = json.dumps(step["body"]).encode()
            headers["Content-Type"] = "application/json"
        connection.request(step["method"], step["path"], body=body, headers=headers)
        response = connection.getresponse()
        results.append({
            "status": response.status,
            "headers": [[k.lower(), v] for k, v in response.getheaders()],
            "body": response.read().decode(errors="replace"),
        })
    except Exception as error:
        results.append({"error": type(error).__name__})
    finally:
        connection.close()
print(json.dumps(results))
"""
