"""SERA Browser Agent — Playwright-first autonomous browser agent.

A standalone, production-quality autonomous browser agent whose core engine
is Playwright. Receives a high-level objective, drives a real browser, reasons
from a single structured browser state, escalates to vision only when needed,
and returns a deterministic structured report.

Public API:
    from sera_browser import BrowserAgent, SessionManager, Settings
    from sera_browser import create_app  # FastAPI app factory

Quick start:
    settings = Settings(local_lab=True, allowed_private_targets="127.0.0.1")
    bm = BrowserManager(settings)
    sm = SessionManager(settings, bm)
    rec = await sm.create(TaskCreate(target="http://localhost:3000", task="analyze"))
"""
from __future__ import annotations

__version__ = "4.0.0"

# Public API — these are the ONLY imports SERA should use.
# Internal module paths may change; the public API is stable.

# Configuration
from .config import Settings, get_settings

# Browser management
from .browser.manager import BrowserManager

# Session management
from .session.manager import SessionManager, SessionStatus, SessionRecord

# Task + agent
from .task.agent import BrowserAgent, TaskConfig, TaskResult
from .task.planner import TaskPlanner, Step, StepKind
from .task.executor import ActionExecutor, ActionResult
from .task.loop import LoopRecoveryManager

# Models (structured types)
from .models import (
    AgentReport,
    BrowserState,
    InteractiveElement,
    FormDescriptor,
    LinkDescriptor,
    NetworkRequest,
    WebSocketEvent,
    Challenge,
    TaskCreate,
    TaskCreatedResponse,
    AuthenticationSummary,
    NetworkSummary,
)

# LLM layer (provider-agnostic)
from .task.llm_client import (
    LLMClient,
    LLMConfig,
    LLMResponse,
    LLMError,
    LLMUnavailableError,
    LLMTimeoutError,
    LLMAuthError,
    LLMRateLimitError,
    LLMResponseError,
    ZaiLLMClient,
    OpenAICompatLLMClient,
    MockLLMClient,
    make_llm_client,
)

# LLM planner (optional, with deterministic fallback)
from .task.llm_planner import LLMTaskPlanner, LLMPlannerStats, SYSTEM_PROMPT

# Advanced features
from .task.memory import MemoryManager
from .task.safety import SafetyGuard, SafetyConfig
from .task.history import HistoryManager, HistoryEntry
from .task.soft_restrict import SoftRestrict
from .task.observability import Observability, ActionSpan
from .task.extraction import ExtractionResult, validate_against_schema
from .task.pav import PlanActValidateOrchestrator, PAVStats

# Browser subsystems
from .browser.driver import PlaywrightDriver
from .browser.inspector import Inspector
from .browser.context import ContextManager
from .browser.screenshots import ScreenshotLayer
from .browser.storage import StorageManager
from .browser.refmap import RefMap, RefEntry, build_refmap
from .browser.resolver import ElementResolver, ResolutionResult
from .browser.tabs import TabManager, TabRecord
from .browser.trimmer import trim_snapshot, TrimResult
from .browser.stealth import StealthLayer, StealthConfig, StealthFingerprint
from .browser.som import SetOfMarkAnnotator, SoMResult, MarkedElement
from .browser.cdp_ax import CDPAccessibilityExtractor
from .browser.viewer import BrowserViewer, StreamEvent, ViewerStats

# Network recorder (reusable security sensor)
from .net.recorder import NetworkRecorder

# Challenge detection
from .challenge.detector import ChallengeDetector

# v4: browser runtime backends (BrowserRuntime abstraction)
from .runtime import (
    BrowserRuntime,
    LocalPlaywrightRuntime,
    KernelRuntime,
    create_runtime,
    select_backend,
)
from .browser.element_registry import ElementRegistry, ElementRecord, RegistrySnapshot
from .task.coverage import CoverageEngine, ExplorationTarget
from .task.auth_state import AuthStateMachine, AuthEvidence, AuthTransition
from .task.app_graph import ApplicationGraph
from .task.evidence import EvidenceLog, StepEvidence
from .task.failure import ActionFailure, FailureKind, classify_exception
from .task.planner import StepFeedback

# API (FastAPI app factory)
from .api.app import create_app

__all__ = [
    "__version__",
    # Config
    "Settings",
    "get_settings",
    # Browser
    "BrowserManager",
    "PlaywrightDriver",
    "Inspector",
    "ContextManager",
    "ScreenshotLayer",
    "StorageManager",
    "RefMap",
    "RefEntry",
    "build_refmap",
    "ElementResolver",
    "ResolutionResult",
    "TabManager",
    "TabRecord",
    "trim_snapshot",
    "TrimResult",
    "StealthLayer",
    "StealthConfig",
    "StealthFingerprint",
    "SetOfMarkAnnotator",
    "SoMResult",
    "MarkedElement",
    "CDPAccessibilityExtractor",
    "BrowserViewer",
    "StreamEvent",
    "ViewerStats",
    # Session
    "SessionManager",
    "SessionStatus",
    "SessionRecord",
    # Task + agent
    "BrowserAgent",
    "TaskConfig",
    "TaskResult",
    "TaskPlanner",
    "Step",
    "StepKind",
    "ActionExecutor",
    "ActionResult",
    "LoopRecoveryManager",
    # Models
    "AgentReport",
    "BrowserState",
    "InteractiveElement",
    "FormDescriptor",
    "LinkDescriptor",
    "NetworkRequest",
    "WebSocketEvent",
    "Challenge",
    "TaskCreate",
    "TaskCreatedResponse",
    "AuthenticationSummary",
    "NetworkSummary",
    # LLM
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
    "LLMTaskPlanner",
    "LLMPlannerStats",
    "SYSTEM_PROMPT",
    # Advanced
    "MemoryManager",
    "SafetyGuard",
    "SafetyConfig",
    "HistoryManager",
    "HistoryEntry",
    "SoftRestrict",
    "Observability",
    "ActionSpan",
    "ExtractionResult",
    "validate_against_schema",
    "PlanActValidateOrchestrator",
    "PAVStats",
    # Network
    "NetworkRecorder",
    # Challenge
    "ChallengeDetector",
    # v4 runtime backends + intelligence modules
    "BrowserRuntime",
    "LocalPlaywrightRuntime",
    "KernelRuntime",
    "create_runtime",
    "select_backend",
    "ElementRegistry",
    "ElementRecord",
    "RegistrySnapshot",
    "CoverageEngine",
    "ExplorationTarget",
    "AuthStateMachine",
    "AuthEvidence",
    "AuthTransition",
    "ApplicationGraph",
    "EvidenceLog",
    "StepEvidence",
    "ActionFailure",
    "FailureKind",
    "classify_exception",
    "StepFeedback",
    # API
    "create_app",
]
