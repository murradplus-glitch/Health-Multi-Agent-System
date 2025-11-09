"""Minimal MCP-compatible server exposing health assistance tools."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

DATA_DIR = Path("data")
LOG_FILE = Path("logs/mcp.log")


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def triage_rules_tool(symptoms: str) -> Dict[str, Any]:
    """Return the most relevant triage rule for the supplied symptoms."""
    rules: List[Dict[str, Any]] = load_json(DATA_DIR / "triage_rules.json")
    symptoms_lower = symptoms.lower()
    for rule in rules:
        if all(keyword.lower() in symptoms_lower for keyword in rule.get("keywords", [])):
            return {
                "severity": rule.get("severity", "self-care"),
                "reasons": rule.get("reasons", []),
                "advice": rule.get("advice", "")
            }
    return {
        "severity": "self-care",
        "reasons": ["No red-flag keywords matched the provided symptoms."],
        "advice": "Monitor at home and seek care if symptoms worsen."
    }


def program_eligibility_tool(profile: Dict[str, Any]) -> Dict[str, Any]:
    """Evaluate Sehat Card and basic programme eligibility rules."""
    rules = load_json(DATA_DIR / "program_eligibility.json")
    sehat_card = rules.get("sehat_card", {})
    result: Dict[str, Any] = {
        "sehat_card": {
            "eligible": False,
            "reasons": [],
            "missing_documents": []
        },
        "basic_schemes": []
    }

    monthly_income = profile.get("monthly_income")
    poverty_score = profile.get("poverty_score")
    documents = set(profile.get("documents", []))

    if monthly_income is not None and monthly_income <= sehat_card.get("income_monthly_threshold", float("inf")):
        if poverty_score is None or poverty_score <= sehat_card.get("poverty_score_threshold", 100):
            result["sehat_card"]["eligible"] = True
            result["sehat_card"]["reasons"].append("Income and poverty score meet programme thresholds.")
    if not result["sehat_card"]["eligible"]:
        if monthly_income is not None:
            if monthly_income > sehat_card.get("income_monthly_threshold", 0):
                result["sehat_card"]["reasons"].append("Monthly income exceeds threshold.")
        if poverty_score is not None and poverty_score > sehat_card.get("poverty_score_threshold", 100):
            result["sehat_card"]["reasons"].append("Poverty score is higher than allowed.")

    missing_docs = [doc for doc in sehat_card.get("required_documents", []) if doc not in documents]
    if missing_docs:
        result["sehat_card"]["missing_documents"] = missing_docs

    eligible_schemes: List[Dict[str, Any]] = []
    for scheme in rules.get("basic_schemes", []):
        criteria = scheme.get("criteria", {})
        passes = True
        for key, value in criteria.items():
            if key == "conditions":
                patient_conditions = set(profile.get("conditions", []))
                if not patient_conditions.intersection(value):
                    passes = False
                    break
            elif key.endswith("_max"):
                field = key[:-4]
                if profile.get(field) is None or profile.get(field) > value:
                    passes = False
                    break
            else:
                if profile.get(key) != value:
                    passes = False
                    break
        if passes:
            eligible_schemes.append({
                "name": scheme.get("name"),
                "benefits": scheme.get("benefits", [])
            })
    result["basic_schemes"] = eligible_schemes
    return result


def facility_lookup_tool(location: str, severity: str) -> List[Dict[str, Any]]:
    """Return facilities matching the requested location and severity."""
    facilities = load_json(DATA_DIR / "facilities.json")
    location_lower = location.lower()
    severity_lower = severity.lower()
    ranked: List[Dict[str, Any]] = []

    for item in facilities:
        city = item.get("city", "").lower()
        supports = [level.lower() for level in item.get("supports_severity", [])]
        score = 0
        if city == location_lower:
            score += 2
        elif location_lower and location_lower in city:
            score += 1
        if severity_lower in supports:
            score += 2
        ranked.append((score, item))

    ranked.sort(key=lambda pair: pair[0], reverse=True)
    return [item for score, item in ranked if score > 0] or [item for _, item in ranked]


def reminder_store_tool(patient_id: str, message: str, due_datetime: str) -> Dict[str, Any]:
    """Persist a reminder entry in the SQLite store."""
    db_path = DATA_DIR / "reminders.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id TEXT NOT NULL,
            message TEXT NOT NULL,
            due_datetime TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    created_at = datetime.utcnow().isoformat() + "Z"
    with conn:
        cursor = conn.execute(
            "INSERT INTO reminders (patient_id, message, due_datetime, created_at) VALUES (?, ?, ?, ?)",
            (patient_id, message, due_datetime, created_at),
        )
        reminder_id = cursor.lastrowid
    conn.close()
    return {
        "id": reminder_id,
        "patient_id": patient_id,
        "message": message,
        "due_datetime": due_datetime,
        "created_at": created_at,
    }


class MCPServer:
    """Very small JSON-RPC server implementing a subset of the MCP spec."""

    def __init__(self) -> None:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(
            filename=LOG_FILE,
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s",
        )
        self.tools = {
            "triage_rules_tool": triage_rules_tool,
            "program_eligibility_tool": program_eligibility_tool,
            "facility_lookup_tool": facility_lookup_tool,
            "reminder_store_tool": reminder_store_tool,
        }

    async def run(self) -> None:
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, os.fdopen(0, "rb", buffering=0))
        writer_transport, writer_protocol = await asyncio.get_running_loop().connect_write_pipe(asyncio.streams.FlowControlMixin, os.fdopen(1, "wb", buffering=0))
        writer = asyncio.StreamWriter(writer_transport, writer_protocol, reader, asyncio.get_running_loop())

        while True:
            line = await reader.readline()
            if not line:
                break
            try:
                message = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError:
                continue
            response = await self.handle_message(message)
            if response is not None:
                writer.write((json.dumps(response) + "\n").encode("utf-8"))
                await writer.drain()

    async def handle_message(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        method = message.get("method")
        msg_id = message.get("id")
        params = message.get("params", {})

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "server": {"name": "health-mcp-server", "version": "0.1.0"},
                    "capabilities": {"tools": True},
                },
            }
        if method == "list_tools":
            tool_infos = []
            for name in self.tools:
                tool_infos.append({
                    "name": name,
                    "description": name.replace("_", " ").title(),
                })
            return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": tool_infos}}
        if method == "call_tool":
            name = params.get("name")
            arguments = params.get("arguments", {})
            handler = self.tools.get(name)
            if handler is None:
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32601, "message": f"Unknown tool: {name}"},
                }
            logging.info("tool_call %s %s", name, json.dumps(arguments, ensure_ascii=False))
            try:
                if name == "triage_rules_tool":
                    result = handler(arguments.get("symptoms", ""))
                elif name == "program_eligibility_tool":
                    result = handler(arguments.get("profile", {}))
                elif name == "facility_lookup_tool":
                    result = handler(arguments.get("location", ""), arguments.get("severity", ""))
                elif name == "reminder_store_tool":
                    result = handler(
                        arguments.get("patient_id", ""),
                        arguments.get("message", ""),
                        arguments.get("due_datetime", ""),
                    )
                else:
                    result = handler(**arguments)
            except Exception as exc:  # pragma: no cover - defensive
                logging.exception("tool_error %s", name)
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32000, "message": str(exc)},
                }
            logging.info("tool_response %s %s", name, json.dumps(result, ensure_ascii=False))
            return {"jsonrpc": "2.0", "id": msg_id, "result": result}

        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32601, "message": f"Unknown method: {method}"},
        }


def main() -> None:
    server = MCPServer()
    asyncio.run(server.run())


if __name__ == "__main__":
    main()
