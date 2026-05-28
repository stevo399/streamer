from streamer import config


def test_curator_chat_model_default():
    assert hasattr(config, "CURATOR_CHAT_MODEL")
    assert isinstance(config.CURATOR_CHAT_MODEL, str)
