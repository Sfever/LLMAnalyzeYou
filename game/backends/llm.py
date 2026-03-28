import openai
import time
from config_reader import config
from rag import rag
from pathlib import Path
import json
SUMMARY_PREFIX="Following is a conversation summary. Use it as context for future interactions.\n\nSummary:\n"
BASE_DIR = Path(__file__).resolve().parent
RENPY_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "change_expression",
            "description": "Change the on-screen expression for a character in Ren'Py.",
            "parameters": {
                "type": "object",
                "properties": {
                    "character": {
                        "type": "string",
                        "description": "The Ren'Py character or image tag to update, for example 'blue'. All in lowercase",
                    },
                    "expression": {
                        "type": "string",
                        "description": "The target expression name, for example 'smile', 'sad', or 'angry'.",
                    },
                },
                "required": ["character", "expression"],
                "additionalProperties": False,
            },
        },
    }
]

class chat:
    def __init__(self, config_path:str="config.json"):
        resolved_config_path = Path(config_path).expanduser()
        if not resolved_config_path.is_absolute():
            resolved_config_path = BASE_DIR / resolved_config_path
        self.config_path = resolved_config_path.resolve()
        self.config = config(str(self.config_path))
        self.client=openai.OpenAI(base_url="https://openrouter.ai/api/v1", api_key=self.config.api_key)
        self.rag_instance = rag(str(self.config_path))
        self.system_prompt=""
        prompt_path = self.config_path.parent / "system.prompt"
        with open(prompt_path, "r", encoding="utf-8") as f:
            self.system_prompt = f.read().strip()

    def close(self):
        close_client = getattr(self.client, "close", None)
        if callable(close_client):
            close_client()
        self.rag_instance.close()

    def should_summarize(self, messages:list[dict]) -> bool:
        return len(messages) > self.config.context_window * 1.5

    def _system_prompt_messages(self, messages:list[dict]) -> list[dict]:
        system_messages = []

        for message in messages:
            if message.get("role") != "system":
                break

            content = message.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            if content.startswith(SUMMARY_PREFIX):
                continue

            system_messages.append({"role": "system", "content": content})

        if system_messages:
            return system_messages
        if self.system_prompt:
            return [{"role": "system", "content": self.system_prompt}]
        return []

    def summarize_messages(self, messages:list[dict]) -> list[dict]:
        recent_messages = messages[-self.config.context_window:]
        system_prompt = self._system_prompt_messages(messages)
        try:
            summary = self.summarize_memory(messages)
        except openai.APIError:
            return [*system_prompt, *recent_messages]
        return [*system_prompt, {"role":"system","content":summary}, *recent_messages]

    def _retry_delay(self, attempt:int) -> float:
        return min(
            self.config.api_retry_max_delay,
            self.config.api_retry_base_delay * (2 ** max(0, attempt - 1)),
        )

    def _with_retry(self, operation, *, action:str):
        last_error = None
        max_retries = max(0, self.config.api_max_retries)
        retryable_errors = (
            openai.APIConnectionError,
            openai.APITimeoutError,
            openai.RateLimitError,
            openai.InternalServerError,
        )

        for attempt in range(1, max_retries + 2):
            try:
                return operation()
            except retryable_errors as error:
                last_error = error
                if attempt > max_retries:
                    break
                time.sleep(self._retry_delay(attempt))

        raise RuntimeError(f"OpenAI request failed after retries during {action}.") from last_error

    def _create_chat_completion(self, *, action:str, **kwargs):
        return self._with_retry(
            lambda: self.client.chat.completions.create(**kwargs),
            action=action,
        )

    def _response_text(self, response) -> str:
        message = response.choices[0].message
        content = message.content
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            text_parts = []
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text")
                else:
                    text = getattr(item, "text", None)
                if text:
                    text_parts.append(text)
            return "\n".join(text_parts).strip()
        return ""

    def _memory_transcript(self, memory:list[dict]) -> str:
        transcript = []
        memory_list = []
        metadata_list = []
        used_memories = []
        used_memories_count = 0
        for item in memory:
            role = item.get("role")
            content = item.get("content")
            add_to_db=True
            if role == "system" and SUMMARY_PREFIX not in str(content):
                continue
            if not isinstance(content, str) or not content.strip():
                continue
            if add_to_db:
                memory_list.append(content)
                metadata_list.append({"role": role})
            speaker = role.capitalize() if isinstance(role, str) else "Unknown"
            used_memories.append({"role": role, "content": content})
            used_memories_count += 1
            if used_memories_count > self.config.context_window:
                break
            transcript.append(f"{speaker}: {content.strip()}")
        if memory_list:
            self.rag_instance.add_memories(memory_list, metadata_list)
        return "\n".join(transcript[:self.config.context_window])

    def _latest_text_content(self, messages:list[dict]) -> str:
        for message in reversed(messages):
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return content
        return ""
    



    def chat_completation(self, messages:list[dict], stream:bool|None=False):
        latest_text = self._latest_text_content(messages)
        rag_results=self.rag_instance.search(latest_text, "cosine") if latest_text else []
        rag_context="\n\nRelevant information from your knowledge base:\n"
        for result in rag_results:
            rag_context+=f"- {result[1]}\n"
        extra_body={
            "reasoning":{
                "effort": self.config.chat_reasoning_effort,
                "exclude": not self.config.chat_expose_reasoning
            }
        }
        messages=messages+[{"role":"system","content":rag_context}] if rag_results else messages
        if stream:
            return self._create_chat_completion(
                action="chat completion",
                model=self.config.chat_model,
                messages=messages,
                stream=True,
                tools=RENPY_TOOLS,
                tool_choice="auto",
                extra_body=extra_body,
            )
        else:
            return self._create_chat_completion(
                action="chat completion",
                model=self.config.chat_model,
                messages=messages,
                tools=RENPY_TOOLS,
                tool_choice="auto",
                extra_body=extra_body,
            )

    def _response_tool_calls(self, response) -> list[dict]:
        message = response.choices[0].message
        tool_calls = getattr(message, "tool_calls", None) or []
        parsed_tool_calls = []

        for tool_call in tool_calls:
            function = getattr(tool_call, "function", None)
            arguments = getattr(function, "arguments", "{}") if function else "{}"
            try:
                parsed_arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
            except json.JSONDecodeError:
                parsed_arguments = {"raw": arguments}
            parsed_tool_calls.append(
                {
                    "id": getattr(tool_call, "id", None),
                    "name": getattr(function, "name", None),
                    "arguments": parsed_arguments,
                }
            )

        return parsed_tool_calls
        
    def _summarize_chat(self, messages:list[dict]):
        extra_body={
            "reasoning":{
                "effort": self.config.summary_reasoning_effort,
                "exclude": True
            }
        }
        self._create_chat_completion(
            action="chat summary",
            model=self.config.summary_model,
            messages=messages,
            max_tokens=self.config.summary_max_tokens,
            temperature=self.config.summary_temperature,
            extra_body=extra_body,
        )

    
    def summarize_memory(self,memory:list[dict]):
        system_prompt=f"Summarize the conversation for future context in under {self.config.summary_max_tokens} tokens, prioritizing user messages over assistant messages even when short; preserve the user's goals, requests, corrections, preferences, constraints, decisions, and unresolved questions; include assistant content only when it contains conclusions, commitments, or information needed for the next turn; discard filler, repetition, and irrelevant detail; keep only corrected versions when errors were fixed; do not invent facts; format as \"User goal:\", \"Key context:\", \"Decisions made:\", and \"Open points:\", and output exactly \"No durable information.\" if nothing is worth keeping; Conversation: "
        transcript = self._memory_transcript(memory)
        if not transcript:
            return SUMMARY_PREFIX + "No durable information."
        messages=[
            {"role":"system","content":system_prompt},
            {"role":"user","content":transcript}
        ]
        response = self._create_chat_completion(
            action="memory summary",
            model=self.config.summary_model,
            messages=messages,
            max_tokens=self.config.summary_max_tokens,
            temperature=self.config.summary_temperature,
        )
        summary= self._response_text(response) or "No durable information."
        return SUMMARY_PREFIX+summary
    
    def renpy_chat(self, messages:list[dict]):
        if self.should_summarize(messages):
            messages=self.summarize_messages(messages)
        response=self.chat_completation(messages, stream=False)
        return response

    def renpy_stream_chat(self, messages:list[dict]):
        if self.should_summarize(messages):
            messages=self.summarize_messages(messages)
        return self.chat_completation(messages, stream=True)

        
def main():
    instance=chat(str(BASE_DIR / "config.json"))
    messages=[{"role":"system","content":"You are a helpful assistant."}]
    system_prompt=messages
    while True:
        try:
            user_input=input("User: ")
            messages.append({"role":"user","content":user_input})
            response=instance.chat_completation(messages=messages, stream=True)
            print("Assistant: ", end="")
            full_response=""
            for chunk in response:
                print(chunk.choices[0].delta.content, end="", flush=True)
                full_response+=chunk.choices[0].delta.content
            print()  # for newline after the response
            messages.append({"role":"assistant","content":full_response})
            if instance.should_summarize(messages):
                print("Summarizing conversation to manage context window...")
                messages=instance.summarize_messages(messages, system_prompt)
        except KeyboardInterrupt:
            print("\nExiting chat.")
            break
    instance.close()


if __name__ == "__main__":
    main()

        