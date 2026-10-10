"""CrewAI integration for Horizon.

This module provides tool output compression for CrewAI agents,
wrapping BaseTool instances so their outputs are automatically
compressed before entering the agent's LLM context.

Components:
    - HorizonToolWrapper: Wraps a single CrewAI BaseTool with compression
    - wrap_tools_with_horizon: Wraps multiple tools at once
    - ToolCompressionMetrics: Per-invocation metrics dataclass
    - ToolMetricsCollector: Aggregates metrics across all invocations

Example:
    from crewai import Agent, Crew, Task
    from crewai.tools.base_tool import tool
    from horizon.integrations.crewai import wrap_tools_with_horizon

    @tool
    def search_db(query: str) -> str:
        \"\"\"Search the database.\"\"\"
        return json.dumps(results)

    wrapped = wrap_tools_with_horizon([search_db])
    agent = Agent(role="Researcher", tools=wrapped, ...)

Install: pip install contextshrink crewai
"""

from .agents import (
    HorizonToolWrapper,
    ToolCompressionMetrics,
    ToolMetricsCollector,
    get_tool_metrics,
    reset_tool_metrics,
    wrap_tools_with_horizon,
)

__all__ = [
    "HorizonToolWrapper",
    "ToolCompressionMetrics",
    "ToolMetricsCollector",
    "wrap_tools_with_horizon",
    "get_tool_metrics",
    "reset_tool_metrics",
]
