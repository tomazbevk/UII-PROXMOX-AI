import json
import inspect
import logging
import re
from typing import Any

import requests

from backend.config.settings import Settings

logger = logging.getLogger(__name__)

# Ensure tools are registered when this module is imported
import backend.ollama.tools  # noqa: F401


# ------------------------------------------------------------------
# Tool registry
# ------------------------------------------------------------------

TOOL_FUNCTIONS: dict[str, callable] = {}


def register_tool(name: str):
    """Decorator to register a function as a tool."""
    def decorator(fn):
        TOOL_FUNCTIONS[name] = fn
        return fn
    return decorator


def run_tool(name: str, args: dict) -> Any:
    """Execute a registered tool and return its result."""
    fn = TOOL_FUNCTIONS.get(name)
    if fn is None:
        return {"error": f"Unknown tool: {name}"}
    try:
        return fn(**args)
    except Exception as e:
        return {"error": f"Tool '{name}' failed: {e}"}


def get_tool_definitions() -> list[dict]:
    """Return all registered tools in OpenAI function-calling format."""
    tools = []
    for name, fn in TOOL_FUNCTIONS.items():
        sig = inspect.signature(fn)
        params = {}
        required = []
        for param_name, param in sig.parameters.items():
            param_type = "string"
            if param.annotation is int:
                param_type = "integer"
            elif param.annotation is float:
                param_type = "number"
            elif param.annotation is bool:
                param_type = "boolean"
            params[param_name] = {"type": param_type, "description": param_name}
            if param.default is inspect.Parameter.empty:
                required.append(param_name)
        tools.append({
            "type": "function",
            "function": {
                "name": name,
                "description": (fn.__doc__ or name).strip(),
                "parameters": {
                    "type": "object",
                    "properties": params,
                    "required": required,
                },
            },
        })
    return tools


# ------------------------------------------------------------------
# Ollama client
# ------------------------------------------------------------------

class OllamaClient:
    """Client for Ollama using the OpenAI-compatible /v1/chat/completions API."""

    def __init__(self, settings: Settings):
        self.base_url = settings.ollama_url.rstrip("/")
        self.model = settings.ollama_model
        self.session = requests.Session()

    def _chat(self, messages: list[dict], tools: list[dict] | None = None, stream: bool = False):
        """Call /v1/chat/completions and return the parsed response."""
        url = f"{self.base_url}/v1/chat/completions"
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": stream,
            "options": {"temperature": 0.2,
                        "num_ctx": 8192},
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        if stream:
            resp = self.session.post(url, json=payload, stream=True, timeout=(15, None))
            resp.raise_for_status()
            resp.encoding = "utf-8"
            return resp
        else:
            resp = self.session.post(url, json=payload, timeout=120)
            resp.raise_for_status()
            return resp.json()

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict[str, Any]:
        """Send messages to the LLM with tools, loop through tool_calls, return final answer.

        Supports OpenAI-style tool_calls and various text-based tool call formats
        (Gemma, Llama, Mistral, etc.).
        """
        history = list(messages)
        max_rounds = 5

        for round_num in range(max_rounds):
            resp = self._chat(history, tools=tools, stream=False)
            if not isinstance(resp, dict):
                raise RuntimeError("Unexpected non-dict response from Ollama")

            choice = resp.get("choices", [{}])[0]
            msg = choice.get("message", {})
            content = msg.get("content", "") or ""

            logger.debug(f"LLM round {round_num}: content={content[:200]!r}, tool_calls={msg.get('tool_calls')}")

            # Append the assistant message
            history.append(msg)

            # 1) Check OpenAI-style tool_calls (Llama, Qwen, etc.)
            tool_calls = self._extract_openai_tool_calls(msg)

            # 2) If no tool_calls, try parsing text-based formats from content
            if not tool_calls:
                tool_calls = self._parse_text_tool_calls(content)

            if not tool_calls:
                # No tool calls — this is the final answer
                return self._parse_content(content)

            logger.info(f"Executing tool calls: {[tc.get('name') for tc in tool_calls]}")

            # Execute each tool call and append results
            for tc in tool_calls:
                tool_name = tc.get("name", "")
                tool_args = tc.get("args", {})
                if isinstance(tool_args, str):
                    try:
                        tool_args = json.loads(tool_args)
                    except json.JSONDecodeError:
                        tool_args = {}

                result = run_tool(tool_name, tool_args)
                tool_content = self._format_tool_result(tool_name, result)

                history.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "name": tool_name,
                    "content": tool_content,
                })

        content = history[-1].get("content", "") if history else ""
        return self._parse_content(content)

    @staticmethod
    def _extract_openai_tool_calls(msg: dict) -> list[dict]:
        """Extract tool calls from the OpenAI-format message field."""
        raw_calls = msg.get("tool_calls")
        if not raw_calls:
            return []
        result = []
        for i, tc in enumerate(raw_calls):
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function", {})
            if isinstance(fn, str):
                try:
                    fn = json.loads(fn)
                except json.JSONDecodeError:
                    fn = {}
            name = fn.get("name", "") or tc.get("name", "")
            if not name:
                continue
            args_raw = fn.get("arguments", {}) or tc.get("args", {})
            if isinstance(args_raw, str):
                try:
                    args = json.loads(args_raw)
                except json.JSONDecodeError:
                    args = {}
            else:
                args = args_raw or {}
            result.append({
                "name": name,
                "args": args,
                "id": tc.get("id", f"call_{i}"),
            })
        return result

    @staticmethod
    def _parse_text_tool_calls(content: str) -> list[dict]:
        """Parse tool calls from free-text content. Handles multiple formats."""
        if not content:
            return []

        tool_calls = []

        # Format 1: XML-style <tool_call>...</tool_call>
        tool_calls.extend(
            OllamaClient._parse_xml_tool_calls(content)
        )

        # Format 2: Function-call style: function_name(args) on its own line
        if not tool_calls:
            tool_calls.extend(
                OllamaClient._parse_function_call_style(content)
            )

        # Format 3: JSON tool call: {"name": "...", "arguments": {...}}
        if not tool_calls:
            tool_calls.extend(
                OllamaClient._parse_json_tool_calls(content)
            )

        # Format 4: Tool-style: Tool: name\nAction Input: {...}
        if not tool_calls:
            tool_calls.extend(
                OllamaClient._parse_action_input_style(content)
            )

        return tool_calls

    @staticmethod
    def _parse_xml_tool_calls(content: str) -> list[dict]:
        """Parse <tool_call>name(args)</tool_call> and <tool_call>{"name":...,"args":...}</tool_call>."""
        tool_calls = []
        for match in re.finditer(r"<tool_call>\s*(.*?)\s*</tool_call>", content, re.DOTALL):
            inner = match.group(1).strip()
            if not inner:
                continue

            # Try: {"name": "...", "arguments": {...}}  (JSON object)
            json_match = re.match(r"\{.*\}", inner, re.DOTALL)
            if json_match:
                try:
                    data = json.loads(json_match.group(0))
                    name = data.get("name", "")
                    args = data.get("arguments") or data.get("args") or data.get("parameters") or {}
                    if name and isinstance(args, dict):
                        tool_calls.append({"name": name, "args": args, "id": f"call_{len(tool_calls)}"})
                        continue
                except json.JSONDecodeError:
                    pass

            # Try: name(args) — function call style
            fn_match = re.match(r"(\w[\w_]*)\s*\((.*)\)", inner, re.DOTALL)
            if fn_match:
                name = fn_match.group(1)
                args_str = fn_match.group(2).strip()
                args = {}
                if args_str:
                    # Try JSON parse of the args
                    try:
                        args = json.loads(args_str)
                    except json.JSONDecodeError:
                        # Try wrapping in braces
                        try:
                            args = json.loads("{" + args_str + "}")
                        except json.JSONDecodeError:
                            args = {}
                tool_calls.append({"name": name, "args": args, "id": f"call_{len(tool_calls)}"})
                continue

            # Plain name with no args
            if re.match(r"^[\w_]+$", inner):
                tool_calls.append({"name": inner, "args": {}, "id": f"call_{len(tool_calls)}"})

        return tool_calls

    @staticmethod
    def _parse_function_call_style(content: str) -> list[dict]:
        """Parse lines like: function_name({"key": "value"}) or function_name()"""
        tool_calls = []
        for match in re.finditer(
            r"^(\w[\w_]*)\s*\((.*?)\)\s*$", content, re.MULTILINE | re.DOTALL
        ):
            name = match.group(1)
            args_str = match.group(2).strip()
            args = {}
            if args_str:
                try:
                    args = json.loads(args_str)
                except json.JSONDecodeError:
                    try:
                        args = json.loads("{" + args_str + "}")
                    except json.JSONDecodeError:
                        args = {}
            tool_calls.append({"name": name, "args": args, "id": f"call_{len(tool_calls)}"})
        return tool_calls

    @staticmethod
    def _parse_json_tool_calls(content: str) -> list[dict]:
        """Parse standalone JSON objects that look like tool calls: {"name": "...", "arguments": {...}}"""
        tool_calls = []
        # Look for JSON objects with "name" and "arguments" keys
        for match in re.finditer(r'\{\s*"name"\s*:\s*"([^"]+)"\s*,\s*"arguments"\s*:\s*(\{.*?\})\s*\}', content, re.DOTALL):
            name = match.group(1)
            try:
                args = json.loads(match.group(2))
                tool_calls.append({"name": name, "args": args, "id": f"call_{len(tool_calls)}"})
            except json.JSONDecodeError:
                tool_calls.append({"name": name, "args": {}, "id": f"call_{len(tool_calls)}"})
        return tool_calls

    @staticmethod
    def _parse_action_input_style(content: str) -> list[dict]:
        """Parse ReAct-style: Tool: name\nAction Input: {...}"""
        tool_calls = []
        for match in re.finditer(
            r"Tool:\s*(\w[\w_]*)\s*\n\s*Action\s*Input:\s*(.*)",
            content,
            re.IGNORECASE | re.DOTALL,
        ):
            name = match.group(1)
            input_str = match.group(2).strip()
            args = {}
            if input_str:
                try:
                    args = json.loads(input_str)
                except json.JSONDecodeError:
                    args = {"query": input_str}
            tool_calls.append({"name": name, "args": args, "id": f"call_{len(tool_calls)}"})
        return tool_calls

    @staticmethod
    def _format_tool_result(tool_name: str, result: Any) -> str:
        """Format a tool result into a concise summary for the LLM."""
        if isinstance(result, dict):
            if "error" in result:
                return json.dumps(result)
            if tool_name == "scan_containers":
                containers = result.get("containers", [])
                count = result.get("count", len(containers))
                running = [c for c in containers if c.get("status") == "running"]
                stopped = [c for c in containers if c.get("status") == "stopped"]
                lines = [f"Container scan: {count} total ({len(running)} running, {len(stopped)} stopped)"]
                for c in containers:
                    ip_info = f", IP: {c['ip']}" if c.get("ip") else ""
                    lines.append(f"  - {c['name']} ({c['type']}, {c['node']}, {c['status']}{ip_info})")
                return "\n".join(lines)
            if tool_name == "get_logs":
                logs = result.get("logs", [])
                container = result.get("container", "all")
                lines = [f"Logs for {container}: {len(logs)} entries"]
                for log in logs[:10]:
                    msg = str(log.get("message", ""))[:120]
                    lines.append(f"  - {msg}")
                return "\n".join(lines)
        # Default: truncate large JSON
        text = json.dumps(result) if not isinstance(result, str) else result
        if len(text) > 2000:
            return text[:2000] + "...(truncated)"
        return text

    def stream_chat(self, messages: list[dict], tools: list[dict] | None = None):
        """Stream the LLM response as SSE lines (no tool loop)."""
        resp = self._chat(messages, tools=tools, stream=True)
        if not isinstance(resp, requests.Response):
            raise RuntimeError("Unexpected response type from Ollama stream")

        for raw in resp.iter_lines(decode_unicode=False):
            if raw is None:
                continue
            line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
            if not line.strip():
                continue
            yield line + "\n"

    def generate_json(self, prompt: str, system_prompt: str = "") -> dict[str, Any]:
        """Legacy: generate structured JSON output (no tool support)."""
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        return self.chat(messages, tools=None)

    def generate_stream(self, prompt: str, system_prompt: str = ""):
        """Legacy: stream raw lines from Ollama (no tool support)."""
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        yield from self.stream_chat(messages, tools=None)

    @staticmethod
    def _parse_content(content: str) -> dict[str, Any]:
        """Try to parse the assistant content as JSON, fall back to raw text."""
        if not content:
            return {"summary": "No response from model.", "reasoning": "", "confidence": 0.0}

        cleaned = content.strip()

        # Try direct JSON parse first
        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

        # Try to extract JSON from markdown code fences
        fence_match = re.search(r"```(?:json)?\s*\n?(.+?)\n?```", cleaned, re.DOTALL)
        if fence_match:
            try:
                parsed = json.loads(fence_match.group(1).strip())
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass

        # Try to find JSON objects with nested braces (iteratively)
        for pattern in [
            r"\{[^{}]*\}",                          # simple: {"key": "value"}
            r"\{[^{}]*\{[^{}]*\}[^{}]*\}",          # one level nested
            r"\{[^{}]*\{[^{}]*\{[^{}]*\}[^{}]*\}[^{}]*\}",  # two levels
        ]:
            for json_match in re.finditer(pattern, cleaned, re.DOTALL):
                try:
                    parsed = json.loads(json_match.group(0))
                    if isinstance(parsed, dict):
                        return parsed
                except json.JSONDecodeError:
                    continue

        # Fall back: wrap the text in the expected schema
        return {
            "summary": cleaned,
            "reasoning": "Model returned text instead of JSON.",
            "confidence": 0.3,
            "suggested_actions": [],
        }

    def list_models(self) -> list[str]:
        """Return installed Ollama model names."""
        url = f"{self.base_url}/api/tags"
        try:
            response = self.session.get(url, timeout=15)
            response.raise_for_status()
            data = response.json()
        except requests.RequestException:
            return []
        return [m.get("name", "") for m in data.get("models", []) if m.get("name")]
