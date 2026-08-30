"""#9: 確定版スタック(設計書 §13)の全パッケージが実際に import できることを検証する。

DEPENDENCY_NOTES.md に記録した2点の理由により、torch/torchaudio は 2.10.0 に、
piano_transcription_inference は audioread を追加した状態でそれぞれ検証している。
"""

import importlib

import pytest

CONFIRMED_STACK_MODULES = [
    "fastapi",
    "uvicorn",
    "pydantic",
    "onnxruntime",
    "librosa",
    "soundfile",
    "scipy",
    "numpy",
    "demucs_onnx",
    "audio_separator",
    "beat_this",
    "torch",
    "torchaudio",
    "piano_transcription_inference",
    "torchcrepe",
    "partitura",
    "music21",
    "mido",
    "anthropic",
    "claude_agent_sdk",
    "mcp",
    "httpx",
    "keyring",
]


@pytest.mark.parametrize("module_name", CONFIRMED_STACK_MODULES)
def test_module_imports_without_exception(module_name: str) -> None:
    importlib.import_module(module_name)


def test_torch_is_cpu_only() -> None:
    """§13: torch は CPU ビルドで足りる(CUDA 不要)。CUDA が紛れ込んでいないことを保証する。"""
    import torch

    assert torch.cuda.is_available() is False
    assert "+cpu" in torch.__version__ or "cu" not in torch.__version__
