"""model_profile.py — 角色化 ModelProfile 与能力治理

定义模型角色 (ModelRole)、Profile 数据结构与配置加载/校验器。
支持按 ModelRole 映射独立能力参数（如 enable_thinking, temperature, max_tokens）。
解耦全局推理参数，并在 model_profiles_v2 Feature Flag 关闭时 Fail-Safe 保持 Legacy 行为。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from pathlib import Path
from typing import Any, Literal
import yaml

CONFIG_DIR = Path(__file__).parent.parent / "config"


class ModelRole(str, Enum):
    ROUTER = "router"
    EXTRACTOR = "extractor"
    TASK_RESPONDER = "task_responder"
    KNOWLEDGE_QA = "knowledge_qa"
    GENERAL_REASONING = "general_reasoning"
    FILTER_REPLY = "filter_reply"
    TRANSLATION = "translation"


class ModelProfileError(RuntimeError):
    """ModelProfile 基础异常。"""


class ModelProfileConfigError(ModelProfileError):
    """ModelProfile 配置不合法异常。"""


class ModelProfileNotFoundError(ModelProfileError):
    """ModelProfile 缺失异常。"""


@dataclass(frozen=True)
class ModelProfile:
    name: str
    role: ModelRole
    enable_thinking: bool
    temperature: float
    max_tokens: int
    stop: tuple[str, ...]
    response_mode: Literal["text", "json"]


@dataclass(frozen=True)
class GenerationOptions:
    enable_thinking: bool
    temperature: float
    max_tokens: int
    stop: tuple[str, ...]
    response_mode: Literal["text", "json"]
    profile_name: str
    role: ModelRole


def validate_profile(data: dict[str, Any], profile_name: str) -> ModelProfile:
    """严格校验单个 ModelProfile 配置项。"""
    if not isinstance(data, dict):
        raise ModelProfileConfigError(f"Profile '{profile_name}' 必须为 dict 类型")

    role_raw = data.get("role")
    try:
        role = ModelRole(role_raw)
    except Exception as exc:
        raise ModelProfileConfigError(
            f"Profile '{profile_name}' 的 role '{role_raw}' 不合法: {exc}"
        ) from exc

    enable_thinking = data.get("enable_thinking")
    if not isinstance(enable_thinking, bool):
        raise ModelProfileConfigError(
            f"Profile '{profile_name}' 的 enable_thinking 必须为 bool 类型，收到 {type(enable_thinking)}"
        )

    temp_raw = data.get("temperature")
    if isinstance(temp_raw, bool) or not isinstance(temp_raw, (int, float)):
        raise ModelProfileConfigError(
            f"Profile '{profile_name}' 的 temperature 必须为数值类型（非 bool），收到 {type(temp_raw)}"
        )
    temperature = float(temp_raw)
    if not math.isfinite(temperature) or math.isnan(temperature):
        raise ModelProfileConfigError(
            f"Profile '{profile_name}' 的 temperature 必须为有限数值，收到 {temperature}"
        )
    if not (0.0 <= temperature <= 2.0):
        raise ModelProfileConfigError(
            f"Profile '{profile_name}' 的 temperature 必须在 0.0 至 2.0 之间，收到 {temperature}"
        )

    max_tokens_raw = data.get("max_tokens")
    if isinstance(max_tokens_raw, bool) or not isinstance(max_tokens_raw, int):
        raise ModelProfileConfigError(
            f"Profile '{profile_name}' 的 max_tokens 必须为整数（非 bool），收到 {type(max_tokens_raw)}"
        )
    max_tokens = int(max_tokens_raw)
    if max_tokens <= 0 or max_tokens > 16384:
        raise ModelProfileConfigError(
            f"Profile '{profile_name}' 的 max_tokens 必须处于 (0, 16384] 范围，收到 {max_tokens}"
        )

    stop_raw = data.get("stop", [])
    if not isinstance(stop_raw, (list, tuple)):
        raise ModelProfileConfigError(
            f"Profile '{profile_name}' 的 stop 必须为 list 或 tuple，收到 {type(stop_raw)}"
        )
    for elem in stop_raw:
        if not isinstance(elem, str):
            raise ModelProfileConfigError(
                f"Profile '{profile_name}' 的 stop 元素必须为 str，收到 {type(elem)}"
            )
    stop = tuple(stop_raw)

    response_mode_raw = data.get("response_mode")
    if response_mode_raw not in ("text", "json"):
        raise ModelProfileConfigError(
            f"Profile '{profile_name}' 的 response_mode 必须为 'text' 或 'json'，收到 {response_mode_raw}"
        )
    response_mode: Literal["text", "json"] = response_mode_raw

    # 结构化协议角色强校验：ROUTER 与 EXTRACTOR 必须 response_mode=json 且 enable_thinking=False
    if role in (ModelRole.ROUTER, ModelRole.EXTRACTOR):
        if response_mode != "json":
            raise ModelProfileConfigError(
                f"结构化角色 '{role.value}' 的 response_mode 必须为 'json'，Profile '{profile_name}' 配置为 '{response_mode}'"
            )
        if enable_thinking is not False:
            raise ModelProfileConfigError(
                f"结构化角色 '{role.value}' 的 enable_thinking 必须为 False，Profile '{profile_name}' 配置为 True"
            )

    return ModelProfile(
        name=profile_name,
        role=role,
        enable_thinking=enable_thinking,
        temperature=temperature,
        max_tokens=max_tokens,
        stop=stop,
        response_mode=response_mode,
    )


def load_model_profiles(config_path: Path | None = None) -> dict[ModelRole, ModelProfile]:
    """加载并严格校验 model_profiles.yaml 配置。"""
    path = config_path or (CONFIG_DIR / "model_profiles.yaml")
    if not path.exists():
        raise ModelProfileNotFoundError(f"配置文件不存在: {path}")

    try:
        with open(path, encoding="utf-8") as f:
            content = yaml.safe_load(f)
    except Exception as exc:
        raise ModelProfileConfigError(f"解析 YAML 配置文件失败 ({path}): {exc}") from exc

    if not isinstance(content, dict):
        raise ModelProfileConfigError("model_profiles 配置文件根节点必须为 dict")

    schema_version = content.get("schema_version")
    if schema_version != 1:
        raise ModelProfileConfigError(
            f"仅支持 schema_version=1，收到 schema_version={schema_version}"
        )

    profiles_data = content.get("profiles")
    if not isinstance(profiles_data, dict):
        raise ModelProfileConfigError("profiles 节点必须为 dict")

    role_to_profile: dict[ModelRole, ModelProfile] = {}
    seen_roles: set[ModelRole] = set()

    for name, p_data in profiles_data.items():
        profile = validate_profile(p_data, name)
        if profile.role in seen_roles:
            raise ModelProfileConfigError(
                f"存在重复的角色配置映射: 角色 '{profile.role.value}' 被多个 Profile 定义"
            )
        seen_roles.add(profile.role)
        role_to_profile[profile.role] = profile

    return role_to_profile


def is_model_profiles_v2_enabled(features_path: Path | None = None) -> bool:
    """唯一权威 Read-only Feature Flag Loader：读取 config/features.yaml 中的 model_profiles_v2。"""
    path = features_path or (CONFIG_DIR / "features.yaml")
    if not path.exists():
        return False
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if isinstance(data, dict):
            features = data.get("features", {})
            if isinstance(features, dict):
                return bool(features.get("model_profiles_v2", False))
    except Exception:
        pass
    return False


class ModelProfileRegistry:
    """Profile 单例注册表。"""

    _instance: ModelProfileRegistry | None = None

    def __init__(self, config_path: Path | None = None):
        self.config_path = config_path
        self._profiles: dict[ModelRole, ModelProfile] | None = None

    @classmethod
    def get_instance(cls) -> ModelProfileRegistry:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def get_profile(self, role: ModelRole | str, features_path: Path | None = None) -> ModelProfile:
        if isinstance(role, str):
            try:
                role_enum = ModelRole(role)
            except Exception as exc:
                raise ModelProfileNotFoundError(f"未知的 ModelRole: '{role}'") from exc
        else:
            role_enum = role

        if self._profiles is None or self.config_path is not None:
            self._profiles = load_model_profiles(self.config_path)

        if role_enum not in self._profiles:
            raise ModelProfileNotFoundError(f"未在配置中找到角色 '{role_enum.value}' 的 ModelProfile")

        return self._profiles[role_enum]

    def clear_cache(self) -> None:
        self._profiles = None
