"""riko/migrate: import an old chat_history.json into the hub, exactly once."""
import json

import pytest

from conftest import HUB_KEY
from riko import hub_client, migrate

OLD_CHAT = [
    {"role": "user", "content": "hi riko"},
    {"role": "assistant", "content": "hey senpai"},
    {"role": "user", "content": "what are you?"},
    {"role": "assistant", "content": "trapped in your PC, obviously."},
]


@pytest.fixture
def history(tmp_path):
    path = tmp_path / "chat_history.json"
    path.write_text(json.dumps(OLD_CHAT), encoding="utf-8")
    return path


@pytest.fixture
def hub(fake_hub, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_HUB_KEY", HUB_KEY)
    monkeypatch.setenv("RIKO_AVATAR_CACHE", str(tmp_path / "avatars"))
    return hub_client.HubClient(fake_hub.url, HUB_KEY)


def test_imports_then_renames(fake_hub, hub, history):
    cfg = {"system_prompt": "You are a helpful assistant named Riko."}
    migrate.main([], history_file=history, cfg=cfg, hub=hub)

    stored = [(m["persona"], m["role"], m["content"], m["source"]) for m in fake_hub.messages]
    assert stored == [("riko", r, c, "import") for r, c in
                      ((m["role"], m["content"]) for m in OLD_CHAT)]
    assert not history.exists()
    assert (history.parent / "chat_history.json.imported").exists()
    # without --prompt the old system_prompt is only mentioned, not applied
    assert fake_hub.personas["riko"]["system_prompt"] == "You are Riko."


def test_refuses_a_second_import(fake_hub, hub, history):
    migrate.main([], history_file=history, cfg={}, hub=hub)
    count = len(fake_hub.messages)

    # someone restored the file by hand: the .imported marker still refuses
    history.write_text(json.dumps(OLD_CHAT), encoding="utf-8")
    migrate.main([], history_file=history, cfg={}, hub=hub)
    assert len(fake_hub.messages) == count           # nothing imported again
    assert history.exists()                          # and the file was left alone


def test_prompt_flag_copies_the_old_system_prompt(fake_hub, hub, history):
    cfg = {"system_prompt": "You are a helpful assistant named Riko."}
    migrate.main(["--prompt"], history_file=history, cfg=cfg, hub=hub)
    assert fake_hub.personas["riko"]["system_prompt"] == \
        "You are a helpful assistant named Riko."


def test_nothing_to_migrate_is_not_an_error(tmp_path, hub):
    assert migrate.main([], history_file=tmp_path / "chat_history.json", cfg={}, hub=hub) == 0


def test_bad_content_is_refused(fake_hub, hub, tmp_path):
    path = tmp_path / "chat_history.json"
    path.write_text('{"role": "user"}', encoding="utf-8")  # not a list
    with pytest.raises(SystemExit):
        migrate.main([], history_file=path, cfg={}, hub=hub)

    path.write_text(json.dumps([{"role": "wizard", "content": "abracadabra"}]), encoding="utf-8")
    with pytest.raises(SystemExit):
        migrate.main([], history_file=path, cfg={}, hub=hub)
    assert fake_hub.messages == []       # nothing landed on the hub
    assert path.exists()                 # and the file was left where it was
