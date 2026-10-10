"""
Horizon - The Context Optimization Layer for LLM Applications.

Cut your LLM costs by 50-90% without losing accuracy.

Horizon wraps LLM clients to provide:
- Smart compression of tool outputs (keeps errors, anomalies, relevant items)
- Cache-aligned prefix optimization for better provider cache hits
- Rolling window token management for long conversations
- Full streaming support with zero accuracy loss

Quick Start:

    from horizon import HorizonClient, OpenAIProvider
    from openai import OpenAI

    # Wrap your existing client
    client = HorizonClient(
        original_client=OpenAI(),
        provider=OpenAIProvider(),
        default_mode="optimize",
    )

    # Use exactly like the original client
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "user", "content": "Hello!"},
        ],
    )

    # Check savings
    stats = client.get_stats()
    print(f"Tokens saved: {stats['session']['tokens_saved_total']}")

Verify It's Working:

    # Validate configuration
    result = client.validate_setup()
    if not result["valid"]:
        print("Issues:", result)

    # Enable logging to see what's happening
    import logging
    logging.basicConfig(level=logging.INFO)
    # INFO:horizon.transforms.pipeline:Pipeline complete: 45000 -> 4500 tokens

Simulate Before Sending:

    plan = client.chat.completions.simulate(
        model="gpt-4o",
        messages=large_messages,
    )
    print(f"Would save {plan.tokens_saved} tokens")
    print(f"Transforms: {plan.transforms}")

Error Handling:

    from horizon import HorizonError, ConfigurationError, ProviderError

    try:
        response = client.chat.completions.create(...)
    except ConfigurationError as e:
        print(f"Config issue: {e.details}")
    except HorizonError as e:
        print(f"Horizon error: {e}")

For more examples, see https://github.com/horizon-sdk/horizon/tree/main/examples
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

from ._ort import ensure_ort_dylib_pinned
from ._version import __version__  # noqa: F401

# Must run before anything can import `horizon._core`: on Windows the
# Rust core resolves onnxruntime.dll at runtime (ort load-dynamic), and
# the bare DLL search lands on the Windows ML System32 build, which
# deadlocks ort session init (Win11 24H2+). Windows-gated, idempotent,
# ~microseconds. See `horizon/_ort.py` for the full story.
ensure_ort_dylib_pinned()

from .compress import (  # noqa: E402
    CompressConfig,
    CompressResult,
    compress,
    compress_spreadsheet,
    densify,
)

# Keep a real callable bound for the one-function compression API so
# `from horizon import compress` is never shadowed by the submodule object.

__all__ = [
    # Main client
    "HorizonClient",
    # Providers
    "Provider",
    "TokenCounter",
    "OpenAIProvider",
    "AnthropicProvider",
    # Exceptions
    "HorizonError",
    "ConfigurationError",
    "ProviderError",
    "StorageError",
    "CompressionError",
    "TokenizationError",
    "CacheError",
    "ValidationError",
    "TransformError",
    # Config
    "HorizonConfig",
    "HorizonMode",
    "SmartCrusherConfig",
    "CacheAlignerConfig",
    "CacheOptimizerConfig",
    "RelevanceScorerConfig",
    # Data models
    "Block",
    "CachePrefixMetrics",
    "DiffArtifact",
    "RequestMetrics",
    "SimulationResult",
    "TransformDiff",
    "TransformResult",
    "WasteSignals",
    # Transforms
    "SmartCrusher",
    "CacheAligner",
    "TransformPipeline",
    # Cache optimizers
    "BaseCacheOptimizer",
    "CacheConfig",
    "CacheMetrics",
    "CacheResult",
    "CacheStrategy",
    "OptimizationContext",
    "CacheOptimizerRegistry",
    "AnthropicCacheOptimizer",
    "OpenAICacheOptimizer",
    "GoogleCacheOptimizer",
    "SemanticCache",
    "SemanticCacheLayer",
    # Relevance scoring - BM25 always available, embeddings require sentence-transformers
    "RelevanceScore",
    "RelevanceScorer",
    "BM25Scorer",
    "EmbeddingScorer",
    "HybridScorer",
    "create_scorer",
    "embedding_available",
    # Utilities
    "Tokenizer",
    "count_tokens_text",
    "count_tokens_messages",
    "generate_report",
    # Observability
    "HorizonOtelMetrics",
    "HorizonTracer",
    "LangfuseTracingConfig",
    "OTelMetricsConfig",
    "configure_otel_metrics",
    "configure_langfuse_tracing",
    "get_horizon_tracer",
    "get_langfuse_tracing_status",
    "get_otel_metrics",
    "get_otel_metrics_status",
    "reset_horizon_tracing",
    "reset_otel_metrics",
    # Memory - optional hierarchical memory system
    "with_memory",  # Main user-facing API
    "Memory",
    "ScopeLevel",
    "HierarchicalMemory",
    "MemoryConfig",
    "EmbedderBackend",
    # One-function compression API
    "compress",
    "densify",
    "compress_spreadsheet",
    "CompressConfig",
    "CompressResult",
    # Reverse densified tool output (for consumers of mode="agent" output)
    "expand_compacted",
    "is_compacted",
    # Hooks
    "CompressionHooks",
    "CompressContext",
    "CompressEvent",
    # Canonical pipeline
    "PipelineStage",
    "PipelineEvent",
    "PipelineExtensionManager",
    "CANONICAL_PIPELINE_STAGES",
    # Shared context for multi-agent workflows
    "SharedContext",
]

# Keep package-level imports lightweight so `import horizon` does not eagerly
# load provider SDKs, ML stacks, or optional proxy/runtime integrations.
_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    # Main client
    "HorizonClient": ("horizon.client", "HorizonClient"),
    # Providers
    "Provider": ("horizon.providers", "Provider"),
    "TokenCounter": ("horizon.providers", "TokenCounter"),
    "OpenAIProvider": ("horizon.providers", "OpenAIProvider"),
    "AnthropicProvider": ("horizon.providers", "AnthropicProvider"),
    # Exceptions
    "HorizonError": ("horizon.exceptions", "HorizonError"),
    "ConfigurationError": ("horizon.exceptions", "ConfigurationError"),
    "ProviderError": ("horizon.exceptions", "ProviderError"),
    "StorageError": ("horizon.exceptions", "StorageError"),
    "CompressionError": ("horizon.exceptions", "CompressionError"),
    "TokenizationError": ("horizon.exceptions", "TokenizationError"),
    "CacheError": ("horizon.exceptions", "CacheError"),
    "ValidationError": ("horizon.exceptions", "ValidationError"),
    "TransformError": ("horizon.exceptions", "TransformError"),
    # Config
    "HorizonConfig": ("horizon.config", "HorizonConfig"),
    "HorizonMode": ("horizon.config", "HorizonMode"),
    "SmartCrusherConfig": ("horizon.config", "SmartCrusherConfig"),
    "CacheAlignerConfig": ("horizon.config", "CacheAlignerConfig"),
    "CacheOptimizerConfig": ("horizon.config", "CacheOptimizerConfig"),
    "RelevanceScorerConfig": ("horizon.config", "RelevanceScorerConfig"),
    # Data models
    "Block": ("horizon.config", "Block"),
    "CachePrefixMetrics": ("horizon.config", "CachePrefixMetrics"),
    "DiffArtifact": ("horizon.config", "DiffArtifact"),
    "RequestMetrics": ("horizon.config", "RequestMetrics"),
    "SimulationResult": ("horizon.config", "SimulationResult"),
    "TransformDiff": ("horizon.config", "TransformDiff"),
    "TransformResult": ("horizon.config", "TransformResult"),
    "WasteSignals": ("horizon.config", "WasteSignals"),
    # Transforms
    "SmartCrusher": ("horizon.transforms", "SmartCrusher"),
    "CacheAligner": ("horizon.transforms", "CacheAligner"),
    "TransformPipeline": ("horizon.transforms", "TransformPipeline"),
    # Cache optimizers
    "BaseCacheOptimizer": ("horizon.cache", "BaseCacheOptimizer"),
    "CacheConfig": ("horizon.cache", "CacheConfig"),
    "CacheMetrics": ("horizon.cache", "CacheMetrics"),
    "CacheResult": ("horizon.cache", "CacheResult"),
    "CacheStrategy": ("horizon.cache", "CacheStrategy"),
    "OptimizationContext": ("horizon.cache", "OptimizationContext"),
    "CacheOptimizerRegistry": ("horizon.cache", "CacheOptimizerRegistry"),
    "AnthropicCacheOptimizer": ("horizon.cache", "AnthropicCacheOptimizer"),
    "OpenAICacheOptimizer": ("horizon.cache", "OpenAICacheOptimizer"),
    "GoogleCacheOptimizer": ("horizon.cache", "GoogleCacheOptimizer"),
    "SemanticCache": ("horizon.cache", "SemanticCache"),
    "SemanticCacheLayer": ("horizon.cache", "SemanticCacheLayer"),
    # Relevance scoring
    "RelevanceScore": ("horizon.relevance", "RelevanceScore"),
    "RelevanceScorer": ("horizon.relevance", "RelevanceScorer"),
    "BM25Scorer": ("horizon.relevance", "BM25Scorer"),
    "EmbeddingScorer": ("horizon.relevance", "EmbeddingScorer"),
    "HybridScorer": ("horizon.relevance", "HybridScorer"),
    "create_scorer": ("horizon.relevance", "create_scorer"),
    "embedding_available": ("horizon.relevance", "embedding_available"),
    # Utilities
    "Tokenizer": ("horizon.tokenizer", "Tokenizer"),
    "count_tokens_text": ("horizon.tokenizer", "count_tokens_text"),
    "count_tokens_messages": ("horizon.tokenizer", "count_tokens_messages"),
    "generate_report": ("horizon.reporting", "generate_report"),
    # Observability
    "HorizonOtelMetrics": ("horizon.observability", "HorizonOtelMetrics"),
    "HorizonTracer": ("horizon.observability", "HorizonTracer"),
    "LangfuseTracingConfig": ("horizon.observability", "LangfuseTracingConfig"),
    "OTelMetricsConfig": ("horizon.observability", "OTelMetricsConfig"),
    "configure_otel_metrics": ("horizon.observability", "configure_otel_metrics"),
    "configure_langfuse_tracing": ("horizon.observability", "configure_langfuse_tracing"),
    "get_horizon_tracer": ("horizon.observability", "get_horizon_tracer"),
    "get_langfuse_tracing_status": ("horizon.observability", "get_langfuse_tracing_status"),
    "get_otel_metrics": ("horizon.observability", "get_otel_metrics"),
    "get_otel_metrics_status": ("horizon.observability", "get_otel_metrics_status"),
    "reset_horizon_tracing": ("horizon.observability", "reset_horizon_tracing"),
    "reset_otel_metrics": ("horizon.observability", "reset_otel_metrics"),
    # One-function API
    "compress": ("horizon.compress", "compress"),
    "densify": ("horizon.compress", "densify"),
    "compress_spreadsheet": ("horizon.compress", "compress_spreadsheet"),
    "expand_compacted": ("horizon.transforms.compaction_codec", "expand_compacted"),
    "is_compacted": ("horizon.transforms.compaction_codec", "is_compacted"),
    # Hooks
    "CompressionHooks": ("horizon.hooks", "CompressionHooks"),
    "CompressContext": ("horizon.hooks", "CompressContext"),
    "CompressEvent": ("horizon.hooks", "CompressEvent"),
    # Canonical pipeline
    "PipelineStage": ("horizon.pipeline", "PipelineStage"),
    "PipelineEvent": ("horizon.pipeline", "PipelineEvent"),
    "PipelineExtensionManager": ("horizon.pipeline", "PipelineExtensionManager"),
    "CANONICAL_PIPELINE_STAGES": ("horizon.pipeline", "CANONICAL_PIPELINE_STAGES"),
    # Shared context
    "SharedContext": ("horizon.shared_context", "SharedContext"),
}

# Memory remains optional and preserves the long-standing behavior of exposing
# `None` when the extra dependencies are not installed.
_OPTIONAL_EXPORTS = {
    "with_memory": ("horizon.memory", "with_memory"),
    "Memory": ("horizon.memory", "Memory"),
    "ScopeLevel": ("horizon.memory", "ScopeLevel"),
    "HierarchicalMemory": ("horizon.memory", "HierarchicalMemory"),
    "MemoryConfig": ("horizon.memory", "MemoryConfig"),
    "EmbedderBackend": ("horizon.memory", "EmbedderBackend"),
}


def __getattr__(name: str) -> Any:
    """Resolve package exports lazily while preserving legacy import paths."""
    module_attr = _LAZY_EXPORTS.get(name)
    if module_attr is not None:
        module_name, attr_name = module_attr
        value = getattr(import_module(module_name), attr_name)
        globals()[name] = value
        return value

    optional_module_attr = _OPTIONAL_EXPORTS.get(name)
    if optional_module_attr is not None:
        module_name, attr_name = optional_module_attr
        try:
            value = getattr(import_module(module_name), attr_name)
        except ImportError:
            value = None
        globals()[name] = value
        return value

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
