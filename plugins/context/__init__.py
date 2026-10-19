"""Structured prompt assembly and context-pressure policies."""

from plugins.context.assembler import (
    SYSTEM_CONTEXT_FRAME_MARKER,
    ContextRenderResult,
    PromptAssembler,
    PromptSectionMeta,
    PromptSectionRender,
    build_context_frame_content,
    build_context_frame_message,
    is_context_frame,
)
from plugins.context.budget import (
    DEFAULT_CONTEXT_TRIM_PLANS,
    ContextTrimPlan,
)

__all__ = [
    "ContextRenderResult",
    "ContextTrimPlan",
    "DEFAULT_CONTEXT_TRIM_PLANS",
    "PromptAssembler",
    "PromptSectionMeta",
    "PromptSectionRender",
    "SYSTEM_CONTEXT_FRAME_MARKER",
    "build_context_frame_content",
    "build_context_frame_message",
    "is_context_frame",
]
