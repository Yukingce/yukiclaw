"""模型供应商配置测试。"""

import pytest
from pydantic import ValidationError

from infra.settings import Settings


def test_model_tiers_are_selected_by_supplier() -> None:
    """选择 qwen 时应自动使用 qwen 的三档模型。"""
    settings = Settings(
        api_key="test-key",
        model_supplier="QWEN",
        _env_file=None,
    )

    assert settings.model_supplier == "qwen"
    assert settings.model_tiers == {
        "strong": "qwen3-max",
        "standard": "qwen3-plus",
        "cheap": "qwen3-turbo",
    }


def test_model_supplier_can_be_selected_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """YUKI_MODEL_SUPPLIER 应切换默认模型映射。"""
    monkeypatch.setenv("YUKI_API_KEY", "test-key")
    monkeypatch.setenv("YUKI_MODEL_SUPPLIER", "qwen")

    settings = Settings(_env_file=None)

    assert settings.model_tiers["strong"] == "qwen3-max"
    assert settings.model_tiers["cheap"] == "qwen3-turbo"


def test_explicit_model_tiers_override_supplier_defaults() -> None:
    """显式模型映射应优先于供应商默认值。"""
    custom_tiers = {
        "strong": "custom-large",
        "standard": "custom-medium",
        "cheap": "custom-small",
    }

    settings = Settings(
        api_key="test-key",
        model_supplier="deepseek",
        model_tiers=custom_tiers,
        _env_file=None,
    )

    assert settings.model_tiers == custom_tiers


def test_unknown_model_supplier_is_rejected() -> None:
    """未知供应商应在启动加载配置时给出清晰错误。"""
    with pytest.raises(ValidationError, match="不支持的模型供应商"):
        Settings(
            api_key="test-key",
            model_supplier="unknown",
            _env_file=None,
        )


def test_model_tiers_must_include_all_routing_tiers() -> None:
    """显式模型映射必须覆盖路由依赖的全部档位。"""
    with pytest.raises(ValidationError, match="model_tiers 缺少必要档位"):
        Settings(
            api_key="test-key",
            model_tiers={"strong": "custom-large"},
            _env_file=None,
        )
