import json
import stat

from garmin_coach_mcp import login


def test_check_without_tokens_exits_1(tmp_path, capsys):
    assert login.main(["--check", "--tokens-dir", str(tmp_path / "none")]) == 1
    assert "MISSING" in capsys.readouterr().out


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


def test_input_problem_detection():
    assert login._input_problem("Žluťoučký kůň 123!") is None
    assert "broken character" in login._input_problem("heslo\udcc3x")
    assert "control character" in login._input_problem("heslo\x7fx")
    assert "control character" in login._input_problem("heslo\x08x")


def test_prompt_rejects_corrupted_input_and_asks_again(monkeypatch, capsys):
    answers = iter(["abc\udcc3def", "správné heslo"])
    monkeypatch.setattr(login.getpass, "getpass", lambda prompt: next(answers))
    assert login._prompt("Garmin password", secret=True) == "správné heslo"
    assert "broken character" in capsys.readouterr().err


def test_login_sends_nothing_after_repeated_corrupted_password(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(login.getpass, "getpass", lambda prompt: "x\udcc3y")

    def must_not_be_called(*args, **kwargs):
        raise AssertionError("Garmin must not be contacted with a corrupted password")

    monkeypatch.setattr(login, "Garmin", must_not_be_called)
    assert login.interactive_login(tmp_path, "user@example.com") == login.EXIT_FAIL
    assert "Nothing was sent" in capsys.readouterr().err


def test_symlinked_parent_is_resolved_like_macos_tmp(tmp_path):
    real = tmp_path / "private-tmp"
    real.mkdir()
    (tmp_path / "tmp").symlink_to(real)
    normalized = login._normalize_tokens_dir(str(tmp_path / "tmp" / "garmin-tokens"))
    assert normalized == real.resolve() / "garmin-tokens"
    assert not any(p.is_symlink() for p in (normalized, *normalized.parents))
    assert login._ensure_writable(normalized) is None


def test_bad_token_dir_fails_before_asking_for_credentials(monkeypatch, tmp_path, capsys):
    target = tmp_path / "elsewhere"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target)
    monkeypatch.setattr(login.getpass, "getpass", lambda prompt: (_ for _ in ()).throw(AssertionError("asked")))
    assert login.interactive_login(link, "user@example.com") == login.EXIT_FAIL
    assert "symlink" in capsys.readouterr().err


def test_jwt_exp_parsing():
    import base64

    payload = base64.urlsafe_b64encode(json.dumps({"exp": 1790000000}).encode()).decode().rstrip("=")
    assert login._jwt_exp(f"h.{payload}.s").year == 2026
    assert login._jwt_exp("opaque") is None
