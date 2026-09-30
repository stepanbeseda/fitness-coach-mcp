from urllib.parse import parse_qs, urlsplit

from conftest import (
    BASE,
    CLAUDE,
    CLAUDE_CB,
    CLAUDE_CODE,
    FUNNEL_ANTHROPIC,
    FUNNEL_PUBLIC,
    GARMIN,
    HEVY,
    OWNER,
    TAILNET,
    pkce_pair,
)


def query(response):
    return parse_qs(urlsplit(response.headers["location"]).query)


def test_full_flow_claude_ai(h):
    verifier, challenge = pkce_pair()
    page = h.start(challenge=challenge)
    assert page.status_code == 200
    assert "Povolit přístup" in page.text and "Hevy" in page.text
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]

    done = h.submit(h.txn(page))
    assert done.status_code == 303
    location = done.headers["location"]
    assert location.startswith(CLAUDE_CB + "?")
    q = query(done)
    assert q["state"] == ["st-123"]
    assert q["iss"] == [BASE]

    tokens = h.exchange(q["code"][0], verifier, resource=HEVY)
    assert tokens.status_code == 200, tokens.text
    body = tokens.json()
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == 3600
    assert body["access_token"] and body["refresh_token"]
    assert tokens.headers["cache-control"] == "no-store"

    listed = h.mcp(body["access_token"])
    assert listed.status_code == 200
    forwarded = h.backends["hevy"].requests[-1]
    assert forwarded.headers["authorization"] == "Bearer backend-secret-hevy"
    assert body["access_token"] not in str(forwarded.headers)
    assert "tailscale-funnel-request" not in forwarded.headers
    assert "x-forwarded-for" not in forwarded.headers


def test_tokens_are_stored_hashed(h):
    tokens = h.tokens()
    dump = "\n".join(h.store._conn.iterdump())
    assert tokens["access_token"] not in dump
    assert tokens["refresh_token"] not in dump


def test_login_page_not_available_through_funnel(h):
    page = h.start(headers=FUNNEL_ANTHROPIC)
    assert page.status_code == 403
    assert "tailnetu" in page.text


def test_login_page_requires_owner_login(h):
    page = h.start(headers={"Tailscale-User-Login": "someone@github"})
    assert page.status_code == 403
    page = h.start(headers={})  # no identity (e.g. tagged node)
    assert page.status_code == 403


def test_unknown_client_and_bad_redirect_never_redirect(h):
    r = h.start(client_id="https://evil.example/client.json")
    assert r.status_code == 400 and "location" not in r.headers
    r = h.start(redirect_uri="https://evil.example/cb")
    assert r.status_code == 400 and "location" not in r.headers


def test_pkce_and_resource_are_mandatory(h):
    r = h.start(code_challenge_method="plain")
    assert r.status_code == 302 and query(r)["error"] == ["invalid_request"]
    r = h.start(challenge="short")
    assert query(r)["error"] == ["invalid_request"]
    r = h.start(resource=None)
    assert query(r)["error"] == ["invalid_target"]
    r = h.start(resource=f"{BASE}/other/mcp")
    assert query(r)["error"] == ["invalid_target"]
    r = h.start(response_type="token")
    assert query(r)["error"] == ["unsupported_response_type"]
    for response in (r,):
        assert query(response)["iss"] == [BASE]


def test_oversized_state_is_rejected(h):
    r = h.start(state="x" * 2000)
    assert r.status_code == 302 and query(r)["error"] == ["invalid_request"]
    assert "state" not in query(r)


def test_resource_is_normalized(h):
    page = h.start(resource="HTTPS://GW.TEST:443/hevy/mcp/")
    assert page.status_code == 200


def test_wrong_password_or_code(h):
    page = h.start()
    txn = h.txn(page)
    r = h.submit(txn, password="wrong password!")
    assert r.status_code == 401 and "Neplatné heslo nebo kód" in r.text
    r = h.submit(txn, code="000000")
    assert r.status_code == 401
    r = h.submit(txn)
    assert r.status_code == 303


def test_totp_code_cannot_be_replayed(h):
    code = h.totp()
    first = h.submit(h.txn(h.start()), code=code)
    assert first.status_code == 303
    second = h.submit(h.txn(h.start()), code=code)
    assert second.status_code == 401


def test_lockout_after_repeated_failures(h):
    for _ in range(5):
        page = h.start()
        assert h.submit(h.txn(page), password="nope nope nope").status_code == 401
    page = h.start()
    locked = h.submit(h.txn(page))
    assert locked.status_code == 429
    assert "zablokované" in locked.text


def test_attempts_per_request_are_limited(h):
    txn = h.txn(h.start())
    for _ in range(4):
        h.submit(txn, password="bad bad bad bad")
    # 5th failure triggers the global lockout as well; the request is spent afterwards
    h.submit(txn, password="bad bad bad bad")
    assert h.submit(txn).status_code in (400, 429)


def test_deny_redirects_with_access_denied(h):
    r = h.submit(h.txn(h.start()), decision="deny")
    assert r.status_code == 303
    assert query(r)["error"] == ["access_denied"]
    assert query(r)["state"] == ["st-123"]


def test_csrf_cookie_is_required(h):
    page = h.start()
    txn = h.txn(page)
    h.client.cookies.clear()
    assert h.submit(txn).status_code == 400


def test_token_endpoint_network_restrictions(h):
    code, verifier = h.authorize()
    assert h.exchange(code, verifier, headers=FUNNEL_PUBLIC).status_code == 403
    # the owner's tailnet devices (Claude Code, mcp-remote) may use it directly
    assert h.exchange(code, verifier, headers=TAILNET).status_code == 200


def test_token_endpoint_rejects_wrong_verifier_and_reuse(h):
    code, verifier = h.authorize()
    bad = h.exchange(code, pkce_pair()[0])
    assert bad.status_code == 400 and bad.json()["error"] == "invalid_grant"
    # the code was consumed by the failed attempt; a second use counts as reuse
    code, verifier = h.authorize()
    first = h.exchange(code, verifier)
    assert first.status_code == 200
    again = h.exchange(code, verifier)
    assert again.json()["error"] == "invalid_grant"
    # reuse revokes everything issued from that code
    assert h.mcp(first.json()["access_token"]).status_code == 401


def test_token_endpoint_binding_checks(h):
    code, verifier = h.authorize()
    r = h.exchange(code, verifier, redirect_uri="https://claude.com/api/mcp/auth_callback")
    assert r.json()["error"] == "invalid_grant"
    code, verifier = h.authorize()
    r = h.exchange(code, verifier, resource=GARMIN)
    assert r.json()["error"] == "invalid_target"
    code, verifier = h.authorize()
    r = h.exchange(code, verifier, client_id=CLAUDE_CODE)
    assert r.json()["error"] == "invalid_grant"
    r = h.exchange("x", "y" * 43, client_id="https://unknown.example/c")
    assert r.status_code == 401 and r.json()["error"] == "invalid_client"


def test_token_endpoint_requires_form_body(h):
    r = h.client.post("/token", json={"grant_type": "authorization_code"}, headers=FUNNEL_ANTHROPIC)
    assert r.status_code == 400 and r.json()["error"] == "invalid_request"


def test_client_secret_basic_is_rejected_for_public_clients(h):
    import base64

    code, verifier = h.authorize()
    creds = base64.b64encode(f"{CLAUDE}:secret".encode()).decode()
    r = h.client.post(
        "/token",
        data={"grant_type": "authorization_code", "code": code, "code_verifier": verifier,
              "redirect_uri": CLAUDE_CB},
        headers={**FUNNEL_ANTHROPIC, "authorization": f"Basic {creds}"},
    )
    assert r.status_code == 401 and r.json()["error"] == "invalid_client"


def test_claude_code_loopback_redirect_any_port(h):
    code, verifier = h.authorize(client_id=CLAUDE_CODE, redirect_uri="http://localhost:53682/callback")
    r = h.exchange(code, verifier, client_id=CLAUDE_CODE, redirect_uri="http://localhost:53682/callback",
                   headers=TAILNET)
    assert r.status_code == 200
    bad = h.start(client_id=CLAUDE_CODE, redirect_uri="http://localhost:53682/other")
    assert bad.status_code == 400
    bad = h.start(client_id=CLAUDE_CODE, redirect_uri="https://localhost:53682/callback")
    assert bad.status_code == 400


def test_token_is_bound_to_one_resource(h):
    hevy = h.tokens(resource=HEVY)["access_token"]
    assert h.mcp(hevy, path="/hevy/mcp").status_code == 200
    wrong = h.mcp(hevy, path="/garmin/mcp")
    assert wrong.status_code == 401
    assert 'error="invalid_token"' in wrong.headers["www-authenticate"]
    garmin = h.tokens(resource=GARMIN)["access_token"]
    assert h.mcp(garmin, path="/garmin/mcp").status_code == 200


def test_refresh_rotation_and_grace(h):
    first = h.tokens()
    r1 = h.refresh(first["refresh_token"])
    assert r1.status_code == 200
    second = r1.json()
    assert second["refresh_token"] != first["refresh_token"]
    assert h.mcp(second["access_token"]).status_code == 200
    # lost-response retry within the grace window still works
    retry = h.refresh(first["refresh_token"])
    assert retry.status_code == 200
    # the newest refresh token keeps working
    assert h.refresh(retry.json()["refresh_token"]).status_code == 200


def test_refresh_reuse_after_grace_revokes_grant(h):
    first = h.tokens()
    second = h.refresh(first["refresh_token"]).json()
    # pretend the first rotation happened long ago
    h.store._conn.execute("UPDATE tokens SET rotated_at = rotated_at - 3600 WHERE rotated_at IS NOT NULL")
    reuse = h.refresh(first["refresh_token"])
    assert reuse.status_code == 400 and reuse.json()["error"] == "invalid_grant"
    assert h.mcp(second["access_token"]).status_code == 401
    assert h.refresh(second["refresh_token"]).json()["error"] == "invalid_grant"


def test_refresh_requires_same_client_and_network(h):
    tokens = h.tokens()
    assert h.refresh(tokens["refresh_token"], client_id=CLAUDE_CODE).json()["error"] == "invalid_grant"
    assert h.refresh(tokens["refresh_token"], headers=FUNNEL_PUBLIC).status_code == 403
    assert h.refresh("garbage").json()["error"] == "invalid_grant"


def test_revocation(h):
    tokens = h.tokens()
    r = h.client.post("/revoke", data={"token": tokens["refresh_token"], "client_id": CLAUDE},
                      headers=FUNNEL_ANTHROPIC)
    assert r.status_code == 200
    assert h.mcp(tokens["access_token"]).status_code == 401
    assert h.refresh(tokens["refresh_token"]).json()["error"] == "invalid_grant"
    # unknown tokens are accepted silently (RFC 7009)
    assert h.client.post("/revoke", data={"token": "nope", "client_id": CLAUDE},
                         headers=FUNNEL_ANTHROPIC).status_code == 200


def test_expired_access_token(h):
    tokens = h.tokens()
    h.store._conn.execute("UPDATE tokens SET expires_at = 1 WHERE kind = 'access'")
    r = h.mcp(tokens["access_token"])
    assert r.status_code == 401
    assert h.refresh(tokens["refresh_token"]).status_code == 200


def test_duplicate_parameters_rejected(h):
    r = h.client.get(
        "/authorize",
        params=[("client_id", CLAUDE), ("client_id", CLAUDE), ("redirect_uri", CLAUDE_CB)],
        headers=TAILNET,
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert OWNER  # silence unused import linters
