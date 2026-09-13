from types import SimpleNamespace

import pytest

from endstone_ninjos_schematics import blockdata_integration as bridge
from test_chunk_loading import _load_plugin_module


def detection_plugin():
    module = _load_plugin_module()
    plugin = object.__new__(module.NinjOSSchematicsPlugin)
    plugin.server = object()
    plugin._blockdata_enabled = True
    plugin._blockdata = None
    plugin._stopping = False
    plugin._tick_counter = 0
    plugin._blockdata_error = ""
    plugin.save_jobs = {}
    plugin.paste_jobs = {}
    warnings, info = [], []
    plugin.logger = SimpleNamespace(warning=warnings.append, info=info.append)
    return module, plugin, warnings, info


def test_provider_registered_after_enable_is_connected_on_next_tick(monkeypatch):
    module, plugin, warnings, info = detection_plugin()
    integration = SimpleNamespace(api_version="0.6.3", adapter_name="native")
    calls = []

    def connect(_server):
        calls.append(plugin._tick_counter)
        if len(calls) == 1:
            raise RuntimeError("service is not registered")
        return integration

    monkeypatch.setattr(module.BlockDataIntegration, "connect", connect)
    plugin._refresh_blockdata()
    assert plugin._blockdata is None
    plugin._tick_counter = 1
    plugin._refresh_blockdata()
    assert plugin._blockdata is integration
    assert plugin._blockdata_error == ""
    assert calls == [0, 1]
    assert len(warnings) == len(info) == 1
    plugin._tick_counter = 201
    plugin._refresh_blockdata()
    assert calls == [0, 1]


def test_missing_provider_retries_are_rate_limited_without_log_spam(monkeypatch):
    module, plugin, warnings, _ = detection_plugin()
    calls = []

    def connect(_server):
        calls.append(plugin._tick_counter)
        raise RuntimeError("native plugin is missing")

    monkeypatch.setattr(module.BlockDataIntegration, "connect", connect)
    for tick in range(202):
        plugin._tick_counter = tick
        plugin._refresh_blockdata()
    assert calls == [0, 1, 101, 201]
    assert len(warnings) == 1
    assert plugin._blockdata_error == "native plugin is missing"


@pytest.mark.parametrize("jobs", ["save_jobs", "paste_jobs"])
def test_detection_waits_until_active_world_operations_finish(monkeypatch, jobs):
    module, plugin, _, _ = detection_plugin()
    integration = SimpleNamespace(api_version="0.6.3", adapter_name="native")
    calls = []
    monkeypatch.setattr(module.BlockDataIntegration, "connect", lambda _server: calls.append(True) or integration)
    getattr(plugin, jobs)["player"] = object()
    plugin._refresh_blockdata()
    plugin._tick_counter = 1
    plugin._refresh_blockdata()
    assert not calls
    assert plugin._blockdata is None
    getattr(plugin, jobs).clear()
    plugin._tick_counter = 101
    plugin._refresh_blockdata()
    assert plugin._blockdata is integration
    assert calls == [True]


@pytest.mark.parametrize("disabled_field", ["_blockdata_enabled", "_stopping"])
def test_disabled_or_stopped_plugin_does_not_probe(monkeypatch, disabled_field):
    module, plugin, _, _ = detection_plugin()
    setattr(plugin, disabled_field, disabled_field == "_stopping")

    def connect(_server):
        pytest.fail("unexpected BlockData probe")

    monkeypatch.setattr(module.BlockDataIntegration, "connect", connect)
    plugin._refresh_blockdata()


@pytest.mark.parametrize("native, expected", [
    (None, "not loaded"),
    (SimpleNamespace(is_enabled=False), "disabled"),
    (SimpleNamespace(is_enabled=True), "has not registered its service"),
])
def test_unregistered_service_error_identifies_native_plugin_state(monkeypatch, native, expected):
    api = SimpleNamespace(__version__="0.6.3", LiveBlockDataAdapter=lambda _server: SimpleNamespace(available=False))
    monkeypatch.setattr(bridge, "import_module", lambda _name: api)
    server = SimpleNamespace(plugin_manager=SimpleNamespace(get_plugin=lambda _name: native))
    with pytest.raises(bridge.BlockDataIntegrationError, match=expected) as caught:
        bridge.BlockDataIntegration.connect(server)
    assert "Python API v0.6.3 imported successfully" in str(caught.value)


def test_compatible_installed_api_has_no_release_version_pin(monkeypatch):
    adapter = SimpleNamespace(available=True, capabilities=lambda: {"block_entity_nbt": True, "adapter": "native"})
    api = SimpleNamespace(
        __version__="9.9.9",
        LiveBlockDataAdapter=lambda _server: adapter,
        BlockDataService=lambda live_adapter: live_adapter,
    )
    monkeypatch.setattr(bridge, "import_module", lambda _name: api)
    integration = bridge.BlockDataIntegration.connect(object())
    assert integration.api_version == "9.9.9"
    assert integration.adapter is adapter


def test_registered_public_fallback_reports_missing_nbt_capability(monkeypatch):
    adapter = SimpleNamespace(available=True, capabilities=lambda: {"adapter": "public", "block_entity_nbt": False})
    api = SimpleNamespace(__version__="0.6.3", LiveBlockDataAdapter=lambda _server: adapter)
    monkeypatch.setattr(bridge, "import_module", lambda _name: api)
    with pytest.raises(bridge.BlockDataIntegrationError, match="registered, but adapter=public"):
        bridge.BlockDataIntegration.connect(object())
