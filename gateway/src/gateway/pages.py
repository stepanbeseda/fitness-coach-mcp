"""Server-rendered HTML for the login/consent and error pages (no JavaScript)."""

from __future__ import annotations

from html import escape
from urllib.parse import urlsplit

from starlette.responses import HTMLResponse

_STYLE = """
:root { color-scheme: light dark; --fg:#1b1f24; --muted:#5b6470; --bg:#f6f7f9; --card:#fff;
        --line:#d9dde3; --accent:#1f6feb; --danger:#b42318; }
@media (prefers-color-scheme: dark) {
  :root { --fg:#e6e8eb; --muted:#9aa4af; --bg:#0f1216; --card:#171b21; --line:#2b313a;
          --accent:#4c8dff; --danger:#ff6b5f; } }
* { box-sizing: border-box; }
body { margin:0; font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; background:var(--bg);
       color:var(--fg); display:flex; min-height:100vh; align-items:center; justify-content:center; padding:16px; }
main { width:100%; max-width:420px; background:var(--card); border:1px solid var(--line);
       border-radius:12px; padding:24px; }
h1 { font-size:1.25rem; margin:0 0 4px; }
p { margin:8px 0; }
.muted { color:var(--muted); font-size:.9rem; }
dl { margin:16px 0; display:grid; grid-template-columns:auto 1fr; gap:4px 12px; font-size:.95rem; }
dt { color:var(--muted); }
dd { margin:0; overflow-wrap:anywhere; }
label { display:block; margin:14px 0 4px; font-weight:600; }
input { width:100%; padding:10px 12px; font-size:1rem; border:1px solid var(--line);
        border-radius:8px; background:transparent; color:inherit; }
.row { display:flex; gap:8px; margin-top:20px; }
button { flex:1; padding:10px 12px; font-size:1rem; border-radius:8px; border:1px solid var(--line);
         background:transparent; color:inherit; cursor:pointer; }
button.primary { background:var(--accent); border-color:var(--accent); color:#fff; font-weight:600; }
.error { color:var(--danger); font-weight:600; }
"""


def _headers(extra_form_action: str | None = None) -> dict[str, str]:
    form_action = "'self'" + (f" {extra_form_action}" if extra_form_action else "")
    return {
        "Content-Security-Policy": (
            "default-src 'none'; style-src 'unsafe-inline'; img-src 'none'; base-uri 'none'; "
            f"frame-ancestors 'none'; form-action {form_action}"
        ),
        "X-Frame-Options": "DENY",
        "Cache-Control": "no-store",
        "Pragma": "no-cache",
    }


def _page(title: str, body: str) -> str:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="robots" content="noindex">'
        f"<title>{escape(title)}</title><style>{_STYLE}</style></head>"
        f"<body><main>{body}</main></body></html>"
    )


def error_page(status: int, title: str, message: str) -> HTMLResponse:
    body = f"<h1>{escape(title)}</h1><p>{escape(message)}</p>"
    return HTMLResponse(_page(title, body), status_code=status, headers=_headers())


def consent_page(
    *,
    txn: str,
    client_name: str,
    client_id: str,
    redirect_uri: str,
    resource_title: str,
    resource: str,
    error: str | None = None,
    status: int = 200,
) -> HTMLResponse:
    redirect = urlsplit(redirect_uri)
    redirect_origin = f"{redirect.scheme}://{redirect.netloc}"
    error_html = f'<p class="error" role="alert">{escape(error)}</p>' if error else ""
    body = f"""
<h1>Allow access?</h1>
<p class="muted">fitness-coach-mcp · OAuth sign-in</p>
{error_html}
<dl>
  <dt>Application</dt><dd>{escape(client_name)}</dd>
  <dt>Client</dt><dd class="muted">{escape(client_id)}</dd>
  <dt>Redirect</dt><dd class="muted">{escape(redirect_origin)}</dd>
  <dt>Access to</dt><dd><strong>{escape(resource_title)}</strong><br><span class="muted">{escape(resource)}</span></dd>
</dl>
<form method="post" action="/authorize" autocomplete="off">
  <input type="hidden" name="txn" value="{escape(txn)}">
  <label for="password">Password</label>
  <input id="password" name="password" type="password" autocomplete="current-password" required autofocus>
  <label for="totp">Authenticator code</label>
  <input id="totp" name="totp" inputmode="numeric" pattern="[0-9 ]{{6,7}}" maxlength="7"
         autocomplete="one-time-code" required>
  <div class="row">
    <button type="submit" name="decision" value="deny" formnovalidate>Deny</button>
    <button type="submit" name="decision" value="approve" class="primary">Allow</button>
  </div>
</form>
"""
    return HTMLResponse(
        _page("Allow access – fitness-coach-mcp", body),
        status_code=status,
        headers=_headers(redirect_origin),
    )
