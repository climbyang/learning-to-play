from ltp import cli


def _setup(tmp_path, monkeypatch, env_text=""):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "settings.yaml").write_text("db_path: data/t.db\n", encoding="utf-8")
    env = tmp_path / ".env"
    env.write_text(env_text, encoding="utf-8")
    monkeypatch.setenv("LTP_CONFIG", str(cfg / "settings.yaml"))
    monkeypatch.setenv("LTP_ENV", str(env))
    for k in ("LG_TV_IP", "LG_CLIENT_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(k, raising=False)
    return env


def test_status_on_empty_db(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)
    cli.main(["status"])
    out = capsys.readouterr().out
    assert "已用 0 / 20 分钟" in out
    assert "模式：观察" in out


def test_write_env_key_replaces_existing_line(tmp_path):
    env = tmp_path / ".env"
    env.write_text("LG_TV_IP=1.2.3.4\nLG_CLIENT_KEY=\nTELEGRAM_BOT_TOKEN=x\n", encoding="utf-8")
    cli._write_env_key(env, "abc")
    assert env.read_text(encoding="utf-8") == "LG_TV_IP=1.2.3.4\nLG_CLIENT_KEY=abc\nTELEGRAM_BOT_TOKEN=x\n"


def test_write_env_key_appends(tmp_path):
    env = tmp_path / ".env"
    env.write_text("LG_TV_IP=1.2.3.4", encoding="utf-8")
    cli._write_env_key(env, "abc")
    assert env.read_text(encoding="utf-8").endswith("LG_TV_IP=1.2.3.4\nLG_CLIENT_KEY=abc\n")
