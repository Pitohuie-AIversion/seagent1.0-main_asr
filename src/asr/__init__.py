"""
asr - SEAgent 语音识别与 ASR 术语标准化纠偏域

包含：
- asr_service: 语音转写服务与音频处理
- asr_normalizer: 基于上下文的油田专业术语纠偏
"""

from .asr_normalizer import (
    TermCandidate,
    TermRule,
    normalize_terminology,
)
from .asr_service import (
    ASRConfig,
    ASRInputError,
    ASRService,
    ASRUnavailableError,
)

__all__ = [
    "TermCandidate",
    "TermRule",
    "normalize_terminology",
    "ASRConfig",
    "ASRInputError",
    "ASRService",
    "ASRUnavailableError",
]
