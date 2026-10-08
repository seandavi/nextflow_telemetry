import pytest

from nf_client.config import ClientConfig


def test_from_yaml_expands_braced_env_vars_and_keeps_bare_shell_vars(tmp_path, monkeypatch):
    monkeypatch.setenv("NF_TEL_ACCOUNT", "cis240955")
    monkeypatch.setenv("NF_TEL_REPO", "/p/repo")
    path = tmp_path / "c.yaml"
    path.write_text(
        "server_url: https://x/api\n"
        "submission:\n"
        "  mode: slurm\n"
        "  template_path: ${NF_TEL_REPO}/templates/submit_slurm.sh.j2\n"
        "  defaults:\n"
        "    account: ${NF_TEL_ACCOUNT}\n"
        "    client_env_setup: export PATH=$HOME/.local/bin:$PATH\n"
    )
    cfg = ClientConfig.from_yaml(path)
    assert str(cfg.submission.template_path) == "/p/repo/templates/submit_slurm.sh.j2"
    assert cfg.submission.defaults["account"] == "cis240955"
    assert cfg.submission.defaults["client_env_setup"] == "export PATH=$HOME/.local/bin:$PATH"


def test_from_yaml_unset_env_var_names_the_key(tmp_path, monkeypatch):
    monkeypatch.delenv("NF_TEL_ACCOUNT", raising=False)
    path = tmp_path / "c.yaml"
    path.write_text("server_url: https://x/api\nsubmission:\n  defaults:\n    account: ${NF_TEL_ACCOUNT}\n")
    with pytest.raises(ValueError, match=r"submission\.defaults\.account.*NF_TEL_ACCOUNT"):
        ClientConfig.from_yaml(path)


def test_sanitized_config_yaml_drops_token_and_defaults():
    cfg = ClientConfig(
        server_url="https://x/api",
        token="s3cret",
        submission={"mode": "slurm", "defaults": {"google_credentials": "/home/me/key.json"}},
    )
    out = cfg.sanitized_config_yaml()
    assert "s3cret" not in out
    assert "key.json" not in out
    assert "server_url" in out


def test_dispatch_workflow_id_accepts_a_comma_separated_string():
    from nf_client.config import DispatchConfig

    assert DispatchConfig(workflow_id="cmgd_humann3.9, cmgd_mpa4.2").workflow_id == ["cmgd_humann3.9", "cmgd_mpa4.2"]
