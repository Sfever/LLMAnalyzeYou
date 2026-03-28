import argparse
import json
import sys
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent


def _configure_stdio():
	for stream_name in ("stdin", "stdout", "stderr"):
		stream = getattr(sys, stream_name, None)
		if stream is None or not hasattr(stream, "reconfigure"):
			continue
		kwargs = {"encoding": "utf-8"}
		if stream_name == "stderr":
			kwargs["errors"] = "replace"
		stream.reconfigure(**kwargs)


def _usage_to_dict(usage):
	if usage is None:
		return None

	if hasattr(usage, "model_dump"):
		return usage.model_dump()

	usage_dict = {}
	for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
		value = getattr(usage, key, None)
		if value is not None:
			usage_dict[key] = value
	return usage_dict or None


def _parse_tool_arguments(arguments):
	if not arguments:
		return {}
	try:
		return json.loads(arguments)
	except json.JSONDecodeError:
		return {"raw": arguments}


class RenPyPipeBackend:
	def __init__(self, config_path: str):
		self.chat_client = None
		self.startup_error = None
		try:
			from llm import chat
			self.chat_client = chat(config_path)
		except Exception as error:
			self.startup_error = error

	def close(self):
		if self.chat_client is not None:
			self.chat_client.close()

	def _raise_startup_error(self):
		if self.startup_error is None:
			return

		error = self.startup_error
		if isinstance(error, ModuleNotFoundError) and getattr(error, "name", "") == "sqlite3":
			raise RuntimeError(
				"The configured backend interpreter does not provide sqlite3. "
				"Set the in-game Python Interpreter setting to a full Python install that includes sqlite3."
			) from error

		raise RuntimeError(f"Backend startup failed: {error}") from error

	def _validate_messages(self, request):
		messages = request
		if not isinstance(messages, list) or not messages:
			raise ValueError("Request payload must be a non-empty JSON array of messages.")
		for index, message in enumerate(messages):
			if not isinstance(message, dict):
				raise ValueError(f"Message at index {index} must be an object.")
			role = message.get("role")
			if not isinstance(role, str) or not role:
				raise ValueError(f"Message at index {index} must include a string 'role'.")
			if role == "assistant" and "content" not in message and "tool_calls" in message:
				continue
			if role == "tool" and "tool_call_id" in message and "content" in message:
				continue
			if "content" not in message:
				raise ValueError(
					f"Message at index {index} must include 'content', or be an assistant tool-call message."
				)
		return messages

	def handle_request(self, request):
		self._raise_startup_error()
		messages = self._validate_messages(request)
		response = self.chat_client.renpy_chat(messages)
		message = response.choices[0].message
		content = self.chat_client._response_text(response)
		return {
			"message": {
				"role": getattr(message, "role", "assistant") or "assistant",
				"content": content,
			},
			"tool_calls": self.chat_client._response_tool_calls(response),
			"model": getattr(response, "model", None),
			"usage": _usage_to_dict(getattr(response, "usage", None)),
		}

	def stream_request(self, request):
		self._raise_startup_error()
		messages = self._validate_messages(request)
		response_stream = self.chat_client.renpy_stream_chat(messages)
		full_content = []
		last_model = None
		tool_calls = {}

		for chunk in response_stream:
			last_model = getattr(chunk, "model", last_model)
			delta = chunk.choices[0].delta
			content = getattr(delta, "content", None)
			delta_tool_calls = getattr(delta, "tool_calls", None) or []
			if not content:
				pass
			else:
				full_content.append(content)
				yield {
					"type": "chunk",
					"content": content,
				}

			for tool_call in delta_tool_calls:
				tool_index = getattr(tool_call, "index", 0)
				entry = tool_calls.setdefault(
					tool_index,
					{"id": None, "name": None, "arguments": ""},
				)
				tool_id = getattr(tool_call, "id", None)
				if tool_id:
					entry["id"] = tool_id
				function = getattr(tool_call, "function", None)
				if function is None:
					continue
				function_name = getattr(function, "name", None)
				if function_name:
					entry["name"] = function_name
				function_arguments = getattr(function, "arguments", None)
				if function_arguments:
					entry["arguments"] += function_arguments

		sorted_tool_calls = []
		for tool_index in sorted(tool_calls):
			entry = tool_calls[tool_index]
			parsed_tool_call = {
				"id": entry["id"],
				"name": entry["name"],
				"arguments": _parse_tool_arguments(entry["arguments"]),
			}
			sorted_tool_calls.append(parsed_tool_call)
			yield {
				"type": "tool_call",
				"tool_call": parsed_tool_call,
			}

		yield {
			"type": "done",
			"message": {
				"role": "assistant",
				"content": "".join(full_content),
			},
			"tool_calls": sorted_tool_calls,
			"model": last_model,
		}


def _write_event(event: dict):
	sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
	sys.stdout.flush()


def run_stdio_loop(backend: RenPyPipeBackend):
	for line in sys.stdin:
		payload = line.strip()
		if not payload:
			continue

		try:
			request = json.loads(payload)
			for response in backend.stream_request(request):
				_write_event(response)
		except Exception as error:
			_write_event(
				{
					"type": "error",
					"error": str(error),
				}
			)


def parse_args():
	parser = argparse.ArgumentParser(
		description="Streaming stdio wrapper for llm.chat.renpy_chat()."
	)
	parser.add_argument("--config", default=str(BASE_DIR / "config.json"))
	return parser.parse_args()


def main():
	_configure_stdio()
	args = parse_args()
	backend = RenPyPipeBackend(args.config)

	try:
		print("Backend ready on stdio.", file=sys.stderr, flush=True)
		run_stdio_loop(backend)
	except KeyboardInterrupt:
		print("Shutting down backend.", file=sys.stderr, flush=True)
	finally:
		backend.close()


if __name__ == "__main__":
	main()
