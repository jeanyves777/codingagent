from app import load_settings


def test_settings_merge_defaults(tmp_path):
    config = tmp_path / "deploy.toml"
    config.write_text('[deploy]\nregion = "us-east-2"\n')
    settings = load_settings(config)
    assert settings.region == "us-east-2" and settings.replicas == 2
