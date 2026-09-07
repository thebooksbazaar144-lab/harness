"""Task subpackage."""
from __future__ import annotations

from .action_schema import (
    ActionValidationError,
    ValidationResult,
    parse_llm_response,
    validate_action,
)
from .agent import BrowserAgent
from .executor import ActionExecutor
from .llm_client import (
    LLMAuthError,
    LLMClient,
    LLMConfig,
    LLMError,
    LLMRateLimitError,
    LLMResponse,
    LLMResponseError,
    LLMTimeoutError,
    LLMUnavailableError,
    MockLLMClient,
    OpenAICompatLLMClient,
    ZaiLLMClient,
    make_llm_client,
)
from .llm_planner import LLMPlannerStats, LLMTaskPlanner, SYSTEM_PROMPT
from .loop import LoopRecoveryManager
from .planner import Step, StepKind, TaskPlanner
from .report import ReportBuilder

__all__ = [
    "BrowserAgent",
    "ActionExecutor",
    "LoopRecoveryManager",
    "TaskPlanner",
    "Step",
    "StepKind",
    "ReportBuilder",
    # LLM layer (provider-agnostic)
    "LLMClient",
    "LLMConfig",
    "LLMResponse",
    "LLMError",
    "LLMUnavailableError",
    "LLMTimeoutError",
    "LLMAuthError",
    "LLMRateLimitError",
    "LLMResponseError",
    "ZaiLLMClient",
    "OpenAICompatLLMClient",
    "MockLLMClient",
    "make_llm_client",
    # LLM planner
    "LLMTaskPlanner",
    "LLMPlannerStats",
    "SYSTEM_PROMPT",
    # Action schema
    "ActionValidationError",
    "ValidationResult",
    "parse_llm_response",
    "validate_action",
]
