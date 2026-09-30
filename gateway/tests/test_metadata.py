from conftest import BASE, FUNNEL_PUBLIC, TAILNET


def test_authorization_server_metadata(h):
    r = h.client.get("/.well-known/oauth-authorization-server", headers=FUNNEL_PUBLIC)
    assert r.status_code == 200
    meta = r.json()
    assert meta["issuer"] == BASE
    assert meta["authorization_endpoint"] == f"{BASE}/authorize"
    assert meta["token_endpoint"] == f"{BASE}/token"
    assert meta["code_challenge_methods_supported"] == ["S256"]
    # CIMD is only used by Claude when both of these are advertised.
    assert meta["client_id_metadata_document_supported"] is True
    assert "none" in meta["token_endpoint_auth_methods_supported"]
    assert meta["authorization_response_iss_parameter_supported"] is True
    assert "registration_endpoint" not in meta


def test_protected_resource_metadata_per_backend(h):
    for name in ("hevy", "garmin"):
        r = h.client.get(f"/.well-known/oauth-protected-resource/{name}/mcp", headers=FUNNEL_PUBLIC)
        assert r.status_code == 200
        meta = r.json()
        assert meta["resource"] == f"{BASE}/{name}/mcp"
        assert meta["authorization_servers"] == [BASE]
        assert meta["bearer_methods_supported"] == ["header"]


def test_root_protected_resource_metadata_is_not_served(h):
    assert h.client.get("/.well-known/oauth-protected-resource").status_code == 404


def test_unauthenticated_mcp_gets_challenge_from_anywhere(h):
    r = h.mcp(None, headers=FUNNEL_PUBLIC)
    assert r.status_code == 401
    challenge = r.headers["www-authenticate"]
    assert challenge.startswith("Bearer ")
    assert f'resource_metadata="{BASE}/.well-known/oauth-protected-resource/hevy/mcp"' in challenge
    assert h.backends["hevy"].requests == []


def test_healthz_only_inside_tailnet(h):
    assert h.client.get("/healthz", headers=TAILNET).json() == {"status": "ok"}
    assert h.client.get("/healthz", headers=FUNNEL_PUBLIC).status_code == 404


def test_unknown_paths_and_security_headers(h):
    r = h.client.get("/admin", headers=FUNNEL_PUBLIC)
    assert r.status_code == 404
    assert r.json() == {"error": "not_found"}
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert "server" not in r.headers or "uvicorn" not in r.headers["server"]


def test_public_rate_limit(make_harness):
    h = make_harness()
    limiter_hits = [h.client.get("/.well-known/oauth-authorization-server", headers=FUNNEL_PUBLIC).status_code
                    for _ in range(400)]
    assert 429 in limiter_hits
