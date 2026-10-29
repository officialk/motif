import json

from app.main import main


def test_dry_run_prints_spec_without_rendering(capsys, tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.CACHE_DIR", tmp_path)
    assert main(["--dry-run", "ambient", "--seed", "3"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["source"] == "procedural" and out["spec"]["genre"] == "ambient" and out["spec"]["seed"] == 3
    assert not list(tmp_path.iterdir())


def test_dry_run_env(capsys, monkeypatch):
    monkeypatch.setenv("MOTIF_DRY_RUN", "1")
    assert main(["house"]) == 0
    assert json.loads(capsys.readouterr().out)["spec"]["genre"] == "house"
