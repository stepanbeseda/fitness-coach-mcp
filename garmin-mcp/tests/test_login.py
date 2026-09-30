import json
import stat

from garmin_coach_mcp import login


def test_check_without_tokens_exits_1(tmp_path, capsys):
    assert login.main(["--check", "--tokens-dir", str(tmp_path / "none")]) == 1
    assert "CHYBÍ" in capsys.readouterr().out


def test_import_writes_private_token_file(tmp_path):
    source = tmp_path / "exported.json"
    source.write_text(json.dumps({"di_token": "a.b.c", "di_refresh_token": "r", "di_client_id": "c"}))
    target = tmp_path / "store"
    assert login.main(["--tokens-dir", str(target), "--import-file", str(source), "--no-verify"]) == 0
    token_file = target / "garmin_tokens.json"
    assert json.loads(token_file.read_text())["di_refresh_token"] == "r"
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


def test_import_rejects_garbage(tmp_path, capsys):
    source = tmp_path / "bad.json"
    source.write_text('{"oauth1_token": "legacy garth format"}')
    assert login.main(["--tokens-dir", str(tmp_path / "store"), "--import-file", str(source), "--no-verify"]) == 1
    assert not (tmp_path / "store" / "garmin_tokens.json").exists()


def test_jwt_exp_parsing():
    import base64

    payload = base64.urlsafe_b64encode(json.dumps({"exp": 1790000000}).encode()).decode().rstrip("=")
    assert login._jwt_exp(f"h.{payload}.s").year == 2026
    assert login._jwt_exp("opaque") is None
