from classcue.local_credentials import delete_api_key, load_api_key, save_api_key


def test_api_key_memory_is_encrypted_and_deletable(tmp_path):
    key_path = tmp_path / "deepseek_api_key.dpapi"

    save_api_key(key_path, "test-key-not-a-real-secret")

    assert key_path.read_bytes() != b"test-key-not-a-real-secret"
    assert load_api_key(key_path) == "test-key-not-a-real-secret"
    assert delete_api_key(key_path) is True
    assert load_api_key(key_path) is None
