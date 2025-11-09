# Health Multi-Agent System

This repository contains tooling and data extracts for health-assistant experiments.

## PDF data extraction

Run the parser to extract citizens, facilities, and Sehat Card rules from the bundled PDF:

```bash
python parse_sehat_pdf.py
```

The script prints three JSON documents (citizens, facilities, and rules) to standard output.

## MCP server

The `mcp_server.py` module exposes health-related tools that conform to the Model Context Protocol (MCP).

### Running locally

1. Ensure Python 3.11+ is available.
2. From the project root, start the server and keep it running while the MCP client connects:
   ```bash
   python mcp_server.py
   ```
3. The server communicates over standard input/output using JSON-RPC. Logs for every tool call and response are written to `logs/mcp.log`.

### Available tools

- `triage_rules_tool(symptoms: str)` – returns severity guidance based on `data/triage_rules.json`.
- `program_eligibility_tool(profile: dict)` – evaluates Sehat Card and basic programme eligibility using `data/program_eligibility.json`.
- `facility_lookup_tool(location: str, severity: str)` – ranks facilities from `data/facilities.json`.
- `reminder_store_tool(patient_id: str, message: str, due_datetime: str)` – stores reminders in `data/reminders.db`.

Each tool invocation and response is appended to `logs/mcp.log` for traceability.
