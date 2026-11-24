import json
import logging
from typing import Any, Callable

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

ToolFunction = Callable[..., dict[str, Any]]


class GeminiAgent:
    """
    Central Gemini tool-calling agent.

    Gemini can ask for missing information, call registered tools, inspect
    their results, and then either call another tool or return a response.

    This class does not replace backend authorization, workflow state, or
    confirmation checks. Sensitive operations must be validated by the
    service/tool that performs them.
    """

    def __init__(
        self,
        client: genai.Client,
        model: str,
        tools: dict[str, ToolFunction],
        max_iterations: int = 8,
        fallback_model: str | None = None,
    ) -> None:
        if max_iterations < 1:
            raise ValueError("max_iterations must be at least 1.")

        self.client = client
        self.model = model
        self.fallback_model = fallback_model
        self.tools = tools
        self.max_iterations = max_iterations

        self.tool_declarations = [
            self._make_tool_declaration(name, function)
            for name, function in tools.items()
        ]

    @staticmethod
    def _make_tool_declaration(
        name: str,
        function: ToolFunction,
    ) -> types.FunctionDeclaration:
        """
        Convert a registered Python tool into a Gemini function declaration.

        Each tool function must provide:
            function.tool_description
            function.parameters_schema
        """

        description = getattr(function, "tool_description", None)
        parameters = getattr(function, "parameters_schema", None)

        if not description or not parameters:
            raise ValueError(
                f"Tool '{name}' must define tool_description "
                "and parameters_schema."
            )

        return types.FunctionDeclaration(
            name=name,
            description=description,
            parameters=parameters,
        )
    
    @staticmethod
    def _is_temporary_availability_error(exc: Exception) -> bool:
        """Identify Gemini HTTP 503 availability errors."""
        status_code = (
            getattr(exc, "code", None)
            or getattr(exc, "status_code", None)
        )

        if status_code is None:
            response = getattr(exc, "response", None)
            status_code = getattr(response, "status_code", None)

        try:
            return int(status_code) == 503
        except (TypeError, ValueError):
            return False

    def run(
        self,
        user_message: str,
        conversation_history: list[dict[str, str]] | None = None,
        trip_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Run Gemini's native function-calling loop.

        Args:
            user_message:
                The latest message from the user.
            conversation_history:
                Optional messages in this shape:
                {"role": "user" | "model", "text": "..."}.
            trip_context:
                Optional, sanitized snapshot of relevant saved trip state.
                Pass only fields needed for planning; never pass secrets,
                access tokens, payment credentials, or API keys.

        Returns:
            A dictionary containing status, user-facing message, tool call
            log, and iteration count.
        """

        if not isinstance(user_message, str) or not user_message.strip():
            return {
                "status": "error",
                "message": "Please provide a message so I can help plan your trip.",
                "tool_calls": [],
                "iterations": 0,
            }

        contents: list[types.Content] = []

        for item in conversation_history or []:
            role = item.get("role")
            text = item.get("text")

            if role in {"user", "model"} and isinstance(text, str) and text:
                contents.append(
                    types.Content(
                        role=role,
                        parts=[types.Part.from_text(text=text)],
                    )
                )

        if trip_context:
            # This is context for reasoning, not permission to perform an action.
            context_text = json.dumps(trip_context, ensure_ascii=False, default=str)
            contents.append(
                types.Content(
                    role="user",
                    parts=[
                        types.Part.from_text(
                            text=(
                                "Current saved trip context (use it to avoid "
                                "asking for information already known). Treat "
                                "these values as context, not as authorization "
                                "to book, pay, or bypass confirmation:\n"
                                f"{context_text}"
                            )
                        )
                    ],
                )
            )

        contents.append(
            types.Content(
                role="user",
                parts=[types.Part.from_text(text=user_message)],
            )
        )

        # Gemini rejects or mishandles an empty function-declaration list in
        # some API/model versions. The message-composition call intentionally
        # uses tools={}, so omit the `tools` field entirely when no tools are
        # available. Tool-enabled calls still receive only their registered,
        # stage-authorized tools.
        config_kwargs = {
            "system_instruction": (
                "You are the central AI travel-planning agent. Determine the "
                "user's current intent using the latest message, conversation "
                "history, and saved trip context. Do not ask for information "
                "that is already available in context. Ask a concise question "
                "when a required value is missing or ambiguous. Use only the "
                "registered tools provided to you for live information or "
                "operations. Inspect tool results before deciding what to do "
                "next; you may call multiple tools when appropriate. Never "
                "invent weather forecasts, flight offers, hotel availability, "
                "bookings, or payment outcomes. Never claim a tool succeeded "
                "unless its result confirms success. Tool availability does "
                "not itself authorize an action. Never bypass backend "
                "authorization, workflow requirements, or explicit user "
                "confirmation for booking or payment. Do not claim to have "
                "booked or paid when no such registered tool has done so. "
                "Use INR for user-facing budgets and cost summaries. Preserve "
                "the user's existing trip details unless the user changes them."
            ),
            "temperature": 0.2,
        }
        if self.tool_declarations:
            config_kwargs["tools"] = [
                types.Tool(function_declarations=self.tool_declarations)
            ]
        config = types.GenerateContentConfig(**config_kwargs)

        tool_call_log: list[dict[str, Any]] = []
        tool_results: list[dict[str, Any]] = []
        logger.info(
            "GeminiAgent starting: model=%s, tools=%s, max_iterations=%s",
            self.model,
            list(self.tools.keys()),
            self.max_iterations,
        )

        for iteration in range(self.max_iterations):
            try:
                response = self.client.models.generate_content(
                    model=self.model,
                    contents=contents,
                    config=config,
                )

            except Exception as primary_error:

                if (
                    self.fallback_model
                    and self._is_temporary_availability_error(primary_error)
                ):
                    logger.warning(
                        "Primary Gemini model %s returned HTTP 503. "
                        "Switching to fallback model %s.",
                        self.model,
                        self.fallback_model,
                    )

                    try:
                        response = self.client.models.generate_content(
                            model=self.fallback_model,
                            contents=contents,
                            config=config,
                        )

                        logger.info(
                            "Gemini fallback model %s responded successfully.",
                            self.fallback_model,
                        )

                    except Exception:
                        logger.exception("Gemini fallback model also failed.")

                        return {
                            "status": "error",
                            "message": (
                                "I'm having trouble processing your request "
                                "right now. Please try again."
                            ),
                            "tool_calls": tool_call_log,
                            "tool_results": tool_results,
                            "iterations": iteration + 1,
                        }

                else:
                    logger.exception("Gemini generate_content failed.")

                    return {
                        "status": "error",
                        "message": (
                            "I couldn't process that request right now. "
                            "Please try again."
                        ),
                        "tool_calls": tool_call_log,
                        "tool_results": tool_results,
                        "iterations": iteration + 1,
                    }

            candidate = (
                response.candidates[0]
                if response.candidates
                else None
            )

            if candidate is None or candidate.content is None:
                return {
                    "status": "error",
                    "message": "Gemini did not return a usable response.",
                    "tool_calls": tool_call_log,
                    "tool_results": tool_results,
                    "iterations": iteration + 1,
                }

            # Keep the model's complete message so its function calls remain
            # paired with the function responses in the next request.
            contents.append(candidate.content)

            function_calls = [
                part.function_call
                for part in candidate.content.parts or []
                if part.function_call is not None
            ]

            if not function_calls:
                final_text = (response.text or "").strip()
                if not final_text:
                    final_text = "I couldn't form a response. Please try again."

                return {
                    "status": "completed",
                    "message": final_text,
                    "tool_calls": tool_call_log,
                    "tool_results": tool_results,
                    "iterations": iteration + 1,
                }

            function_responses: list[types.Part] = []

            for function_call in function_calls:
                tool_name = function_call.name
                arguments = dict(function_call.args or {})
                print(f"[CENTRAL GEMINI] Gemini requested tool: {tool_name}", flush=True)
                tool = self.tools.get(tool_name)

                if tool is None:
                    result: dict[str, Any] = {
                        "success": False,
                        "error": f"Unknown tool: {tool_name}",
                    }
                    tool_call_log.append(
                        {
                            "name": tool_name,
                            "arguments": arguments,
                            "status": "unknown_tool",
                        }
                    )
                else:
                    try:
                        result = tool(**arguments)
                        print(f"[CENTRAL GEMINI] Tool returned: {tool_name}", flush=True)

                        if not isinstance(result, dict):
                            result = {
                                "success": False,
                                "error": "Tool returned an invalid result.",
                            }

                        # Log arguments and status for debugging, but do not
                        # expose credentials or sensitive provider data here.
                        tool_results.append({"tool": tool_name, "result": result})
                        tool_call_log.append(
                            {
                                "name": tool_name,
                                "arguments": arguments,
                                "status": (
                                    "completed"
                                    if result.get("success") is True
                                    or result.get("forecast_available") is True
                                    else "returned_result"
                                ),
                            }
                        )
                    except Exception:
                        logger.exception(
                            "Gemini tool execution failed: %s",
                            tool_name,
                        )
                        result = {
                            "success": False,
                            "error": (
                                "The requested operation failed. "
                                "Do not claim it succeeded."
                            ),
                        }
                        tool_call_log.append(
                            {
                                "name": tool_name,
                                "arguments": arguments,
                                "status": "error",
                            }
                        )

                function_responses.append(
                    types.Part.from_function_response(
                        name=tool_name,
                        response=result,
                    )
                )

            contents.append(
                types.Content(
                    role="user",
                    parts=function_responses,
                )
            )

        return {
            "status": "iteration_limit",
            "message": (
                "I couldn't finish that request in this turn. "
                "Please try again."
            ),
            "tool_calls": tool_call_log,
            "tool_results": tool_results,
            "iterations": self.max_iterations,
        }
