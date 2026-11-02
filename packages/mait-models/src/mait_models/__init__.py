"""MAIT model providers."""

from mait_models.openai_compat import OpenAICompatibleProvider
from mait_models.scripted import ScriptedProvider

__all__ = ["OpenAICompatibleProvider", "ScriptedProvider"]
