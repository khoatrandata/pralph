from __future__ import annotations

import json
import re
from typing import Any

from pralph.models import Story


def extract_json_from_text(text: str) -> Any | None:
    """Multi-strategy JSON extraction from text.

    Tries: direct parse → ```json``` blocks → first balanced {}.
    """
    text = text.strip()

    # Strategy 1: direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Strategy 2: ```json ... ``` fenced blocks
    pattern = r"```(?:json)?\s*\n?(.*?)```"
    matches = re.findall(pattern, text, re.DOTALL)
    for match in matches:
        try:
            return json.loads(match.strip())
        except json.JSONDecodeError:
            continue

    # Strategy 3: first balanced {}
    start = text.find("{")
    if start >= 0:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break

    # Strategy 4: first balanced []
    start = text.find("[")
    if start >= 0:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "[":
                depth += 1
            elif text[i] == "]":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break

    return None


def extract_xml_tag(text: str, tag: str) -> str | None:
    """Extract content between <tag>...</tag>."""
    pattern = rf"<{tag}>(.*?)</{tag}>"
    match = re.search(pattern, text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return None


def detect_completion_signal(text: str) -> bool:
    """Check for [GENERATION_COMPLETE] signal.

    Only matches if the signal appears on its own line (not inside an
    explanation like "is NOT emitted").
    """
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "[GENERATION_COMPLETE]":
            return True
    return False


def detect_loop_complete(text: str) -> bool:
    """Check for [LOOP_COMPLETE] signal.

    Only matches if the signal appears on its own line.
    """
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "[LOOP_COMPLETE]":
            return True
    return False


def detect_ideation_complete(text: str) -> bool:
    """Check for [IDEATION_COMPLETE] signal.

    Only matches if the signal appears on its own line.
    """
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "[IDEATION_COMPLETE]":
            return True
    return False


def parse_plan_output(text: str) -> dict[str, str]:
    """Parse plan phase output — extract changes_summary."""
    summary = extract_xml_tag(text, "changes_summary") or ""
    return {"changes_summary": summary, "raw": text}


def parse_stories_output(text: str) -> tuple[list[Story], bool]:
    """Parse stories phase output.

    Returns (stories, is_complete).
    """
    is_complete = detect_completion_signal(text)

    data = extract_json_from_text(text)
    if data is None:
        return [], is_complete

    stories_data: list[dict] = []

    if isinstance(data, dict):
        stories_data = data.get("stories", [])
        # Also check for additional_stories (research mode)
        stories_data.extend(data.get("additional_stories", []))
    elif isinstance(data, list):
        stories_data = data

    stories: list[Story] = []
    for sd in stories_data:
        if not isinstance(sd, dict):
            continue
        if not sd.get("id"):
            continue
        try:
            story = Story(
                id=sd["id"],
                title=sd.get("title", ""),
                content=sd.get("content", ""),
                acceptance_criteria=sd.get("acceptance_criteria", []),
                priority=sd.get("priority", 3),
                category=sd.get("category", ""),
                complexity=sd.get("complexity", "medium"),
                dependencies=sd.get("dependencies", []),
                source=sd.get("source", "extract"),
                metadata={k: v for k, v in sd.items() if k not in {
                    "id", "title", "content", "acceptance_criteria",
                    "priority", "category", "complexity", "dependencies",
                    "source", "status",
                }},
            )
            stories.append(story)
        except (KeyError, TypeError):
            continue

    return stories, is_complete


def parse_implement_output(text: str) -> dict[str, Any]:
    """Parse implementation phase output — extract status JSON."""
    data = extract_json_from_text(text)
    if isinstance(data, dict) and "status" in data:
        return data

    # Fallback: look for STATUS: markers
    status_match = re.search(r"STATUS:\s*(\w+)", text, re.IGNORECASE)
    if status_match:
        return {"status": status_match.group(1).lower(), "summary": text[:200]}

    # Check for completed_stories (phase1 output)
    if isinstance(data, dict) and "completed_stories" in data:
        return {"status": "implemented", **data}

    return {"status": "error", "reason": "Could not parse implementation output"}


def parse_compound_output(text: str) -> dict[str, Any]:
    """Parse compound capture output — extract captured, reason, solutions.

    Returns dict with:
      - captured (bool)
      - reason (str)
      - solutions (list of dicts)
    """
    data = extract_json_from_text(text)
    if isinstance(data, dict) and "captured" in data:
        return {
            "captured": bool(data["captured"]),
            "reason": data.get("reason", ""),
            "solutions": data.get("solutions", []),
        }

    # Could not parse — default to no capture
    return {
        "captured": False,
        "reason": "Could not parse compound output",
        "solutions": [],
    }


def parse_review_output(text: str) -> dict[str, Any]:
    """Parse review output — extract approved, feedback, issues.

    Returns dict with:
      - approved (bool)
      - feedback (str)
      - issues (list of dicts with severity/description)
    """
    data = extract_json_from_text(text)
    if isinstance(data, dict) and "approved" in data:
        return {
            "approved": bool(data["approved"]),
            "feedback": data.get("feedback", ""),
            "issues": data.get("issues", []),
        }

    # Fallback: look for APPROVED: true/false markers
    approved_match = re.search(r"APPROVED:\s*(true|false)", text, re.IGNORECASE)
    if approved_match:
        approved = approved_match.group(1).lower() == "true"
        return {
            "approved": approved,
            "feedback": text[:500],
            "issues": [],
        }

    # Could not parse — reject to avoid silently passing unreviewed code
    return {
        "approved": False,
        "feedback": "Review output could not be parsed — rejecting for re-review",
        "issues": [],
    }
