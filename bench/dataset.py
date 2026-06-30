"""Benchmark dataset types and loaders."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config import DATASETS_DIR


@dataclass
class ConsultCase:
    id: str
    question: str
    context: str = ""
    category: str = ""
    expected_tools: list[str] = field(default_factory=list)
    # Additional user messages after the first question (multi-turn)
    turns: list[str] = field(default_factory=list)
    # Canned MCP responses available during this case (same schema as tick mock_tools)
    mock_tools: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    type: str = "consult"


@dataclass
class TickCase:
    id: str
    scenario_name: str
    agent_instructions: str
    strategy_instructions: str
    config: dict[str, Any]
    risk_state: dict[str, Any]
    core_data: dict[str, str]
    learnings: str
    summary: str
    recent_decisions: str
    tick_number: int
    mock_tools: dict[str, Any]
    expected_tool_calls: list[str]
    expected_no_calls: list[str] = field(default_factory=list)
    category: str = ""
    tags: list[str] = field(default_factory=list)
    type: str = "tick"


def load_consult_cases(path: Path | None = None) -> list[ConsultCase]:
    path = path or DATASETS_DIR / "consult.jsonl"
    cases = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        data = json.loads(line)
        cases.append(ConsultCase(
            id=data["id"],
            question=data.get("question", ""),
            context=data.get("context", ""),
            category=data.get("category", ""),
            expected_tools=data.get("expected_tools", []),
            turns=data.get("turns", []),
            mock_tools=data.get("mock_tools", {}),
            tags=data.get("tags", []),
            type=data.get("type", "consult"),
        ))
    return cases


def load_tick_cases(path: Path | None = None) -> list[TickCase]:
    path = path or DATASETS_DIR / "tick.jsonl"
    cases = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        data = json.loads(line)
        cases.append(TickCase(
            id=data["id"],
            scenario_name=data["scenario_name"],
            agent_instructions=data["agent_instructions"],
            strategy_instructions=data["strategy_instructions"],
            config=data.get("config", {}),
            risk_state=data.get("risk_state", {}),
            core_data=data.get("core_data", {}),
            learnings=data.get("learnings", ""),
            summary=data.get("summary", ""),
            recent_decisions=data.get("recent_decisions", ""),
            tick_number=data.get("tick_number", 1),
            mock_tools=data.get("mock_tools", {}),
            expected_tool_calls=data.get("expected_tool_calls", []),
            expected_no_calls=data.get("expected_no_calls", []),
            category=data.get("category", ""),
            tags=data.get("tags", []),
        ))
    return cases


def load_all_cases() -> list[ConsultCase | TickCase]:
    return load_consult_cases() + load_tick_cases()
