from nf_client.config import ClientConfig


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
