"""
MinIO Console auto-login proxy endpoints:
  GET /api/minio/login/{namespace}  — Authenticate to namespace-local MinIO Console
                                      and redirect the browser with a session token.
"""
import json
import subprocess
import base64

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.config import logger

router = APIRouter(prefix="/api/minio", tags=["minio"])


def _get_shared_minio_creds() -> tuple[str, str]:
    """
    Read the shared MinIO credentials from the ``mlpipeline-minio-artifact``
    secret in the ``kubeflow`` namespace.  These are the same credentials
    replicated by Metacontroller into every user namespace.
    """
    result_ak = subprocess.run(
        ["kubectl", "get", "secret", "mlpipeline-minio-artifact",
         "-n", "kubeflow", "-o", "jsonpath={.data.accesskey}"],
        capture_output=True, text=True,
    )
    result_sk = subprocess.run(
        ["kubectl", "get", "secret", "mlpipeline-minio-artifact",
         "-n", "kubeflow", "-o", "jsonpath={.data.secretkey}"],
        capture_output=True, text=True,
    )
    if not result_ak.stdout.strip() or not result_sk.stdout.strip():
        raise RuntimeError("Cannot read shared MinIO credentials from kubeflow namespace")

    access_key = base64.b64decode(result_ak.stdout.strip()).decode()
    secret_key = base64.b64decode(result_sk.stdout.strip()).decode()
    return access_key, secret_key


from fastapi.responses import HTMLResponse

@router.get("/login/{namespace}", response_class=HTMLResponse)
def minio_console_login(namespace: str):
    """
    Auto-login proxy for MinIO Console.

    Instead of performing backend-to-backend authentication (which gets blocked
    by the OIDC AuthService / Dex on the gateway and fails to resolve internal DNS),
    this endpoint returns a small HTML page with client-side JavaScript.

    The JavaScript:
    1. Sends a POST request from the browser directly to the namespace's Console
       login API (inheriting the user's active session cookie on the gateway).
    2. Receives the Console session token.
    3. Sets the 'token' cookie for the '/minio/{namespace}/' path.
    4. Redirects the user directly to the Console.
    """
    try:
        access_key, secret_key = _get_shared_minio_creds()
    except RuntimeError as exc:
        logger.error(f"MinIO auto-login credential fetch failed: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))

    html_content = f"""<!DOCTYPE html>
<html>
<head>
    <title>Connecting to MinIO Storage...</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background-color: #f7f9fa;
            display: flex;
            align-items: center;
            justify-content: center;
            height: 100vh;
            margin: 0;
            color: #3c4043;
        }}
        .container {{
            text-align: center;
            background: white;
            padding: 40px;
            border-radius: 8px;
            box-shadow: 0 4px 16px rgba(0,0,0,0.08);
            max-width: 400px;
        }}
        .spinner {{
            border: 4px solid #f3f3f3;
            border-top: 4px solid #1a73e8;
            border-radius: 50%;
            width: 40px;
            height: 40px;
            animation: spin 1s linear infinite;
            margin: 0 auto 20px auto;
        }}
        @keyframes spin {{
            0% {{ transform: rotate(0deg); }}
            100% {{ transform: rotate(360deg); }}
        }}
        .error {{
            color: #d93025;
            display: none;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div id="loader">
            <div class="spinner"></div>
            <h3>Connecting to MinIO Storage...</h3>
            <p style="color: #5f6368;">Establishing secure storage session for namespace <strong>{namespace}</strong>.</p>
        </div>
        <div id="error-box" class="error">
            <h3 style="margin-top: 0;">Connection Failed</h3>
            <p id="error-msg"></p>
            <p><a href="" onclick="window.location.reload(); return false;" style="color: #1a73e8; text-decoration: none; font-weight: bold;">Retry</a></p>
        </div>
    </div>

    <script>
        async function runAutoLogin() {{
            const namespace = "{namespace}";
            const accessKey = "{access_key}";
            const secretKey = "{secret_key}";

            try {{
                // POST to the namespace's local MinIO Console login endpoint relative to the current host
                const response = await fetch(`/minio/${{namespace}}/api/v1/login`, {{
                    method: "POST",
                    headers: {{
                        "Content-Type": "application/json"
                    }},
                    // credentials: "include" ensures the browser stores any Set-Cookie
                    // headers returned by MinIO Console (newer MinIO returns 204 + cookie
                    // instead of 200 + JSON token body)
                    credentials: "include",
                    body: JSON.stringify({{ accessKey, secretKey }})
                }});

                // MinIO >= 2022 returns 204 No Content with a Set-Cookie header.
                // MinIO older versions return 200 with {{"sessionId": "..."}} JSON body.
                // We handle both cases.
                if (response.status === 204) {{
                    // Cookie was set by the browser automatically via Set-Cookie header.
                    // Nothing more to do — just redirect.
                }} else if (response.ok) {{
                    // Older MinIO: parse the JSON token and set it manually.
                    const text = await response.text();
                    if (text && text.trim().length > 0) {{
                        const data = JSON.parse(text);
                        const token = data.sessionId || data.token;
                        if (token) {{
                            document.cookie = `token=${{token}}; path=/minio/${{namespace}}/; SameSite=Lax`;
                        }}
                    }}
                }} else {{
                    const errText = await response.text();
                    throw new Error("MinIO Console returned HTTP " + response.status + ": " + errText);
                }}

                // Redirect to the console UI
                window.location.href = `/minio/${{namespace}}/`;
            }} catch (err) {{
                console.error("Auto-login error:", err);
                document.getElementById("loader").style.display = "none";
                document.getElementById("error-msg").textContent = err.message;
                document.getElementById("error-box").style.display = "block";
            }}
        }}

        runAutoLogin();
    </script>
</body>
</html>
"""
    return HTMLResponse(content=html_content)

