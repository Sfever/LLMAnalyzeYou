import json
from pathlib import Path

DEFAULT_CHAT_MODEL = "openrouter/free"
DEFAULT_CHAT_TEMPERATURE = 0.7
DEFAULT_CHAT_REASONING_EFFORT = "low"
DEFAULT_CHAT_MAX_TOKENS = 4096
DEFAULT_CHAT_EXPOSE_REASONING = False
DEFAULT_EMBEDDING_MODEL = "nvidia/llama-nemotron-embed-vl-1b-v2:free"
DEFAULT_RAG_CHUNK_SIZE = 1000
DEFAULT_RAG_CHUNK_OVERLAP = 200
DEFAULT_RAG_CHUNKING_STRATEGY = "simple"
DEFAULT_RAG_EMBEDDING_BATCH_SIZE = 32
DEFAULT_RAG_TOP_K = 5
DEFAULT_RAG_SIMILARITY_THRESHOLD = 0.2
DEFAULT_RAG_DATABASE = "rag.db"
DEFAULT_MEMORY_CONTEXT_WINDOW = 12
DEFAULT_MEMORY_SUMMARY_MODEL = DEFAULT_CHAT_MODEL
DEFAULT_MEMORY_SUMMARY_TEMPERATURE = 0.3
DEFAULT_MEMORY_SUMMARY_MAX_TOKENS = 1024
DEFAULT_MEMORY_SUMMARY_REASONING_EFFORT = DEFAULT_CHAT_REASONING_EFFORT
DEFAULT_API_MAX_RETRIES = 8
DEFAULT_API_RETRY_BASE_DELAY = 1.0
DEFAULT_API_RETRY_MAX_DELAY = 4


def _resolve_default(value, default_value):
	return default_value if value == "default" or value is None else value


def _as_int(value, default_value):
	resolved = _resolve_default(value, default_value)
	return int(resolved)


def _as_float(value, default_value):
	resolved = _resolve_default(value, default_value)
	return float(resolved)


def _as_bool(value, default_value):
	resolved = _resolve_default(value, default_value)
	if isinstance(resolved, bool):
		return resolved
	if isinstance(resolved, str):
		return resolved.strip().lower() in {"1", "true", "yes", "on"}
	return bool(resolved)


class config:
	def __init__(self, config_path: str = "config.json"):
		config_file = Path(config_path).expanduser()
		if not config_file.is_absolute():
			config_file = config_file.resolve()
		data = json.loads(config_file.read_text(encoding="utf-8")) if config_file.exists() else {}

		chat = data.get("chat", {})
		rag = data.get("rag", {})
		memory = data.get("memory", {})
		summary = memory.get("summary", {})

		self.api_key = data.get("api_key")
		api = data.get("api", {})
		self.chat_model = _resolve_default(chat.get("model"), DEFAULT_CHAT_MODEL)
		self.chat_temperature = _as_float(chat.get("temperature"), DEFAULT_CHAT_TEMPERATURE)
		self.chat_max_tokens = _as_int(chat.get("max_tokens"), DEFAULT_CHAT_MAX_TOKENS)
		self.chat_reasoning_effort = _resolve_default(
			chat.get("reasoning_effort"), DEFAULT_CHAT_REASONING_EFFORT
		)
		self.chat_expose_reasoning = _as_bool(
			chat.get("expose_reasoning"), DEFAULT_CHAT_EXPOSE_REASONING
		)

		self.embedding_model = _resolve_default(rag.get("embedding_model"), DEFAULT_EMBEDDING_MODEL)
		self.chunk_size = _as_int(rag.get("chunk_size"), DEFAULT_RAG_CHUNK_SIZE)
		self.chunk_overlap = _as_int(rag.get("chunk_overlap"), DEFAULT_RAG_CHUNK_OVERLAP)
		self.chunking_strategy = _resolve_default(
			rag.get("chunking_strategy"), DEFAULT_RAG_CHUNKING_STRATEGY
		)
		self.embedding_batch_size = _as_int(
			rag.get("embedding_batch_size"), DEFAULT_RAG_EMBEDDING_BATCH_SIZE
		)
		self.top_k = _as_int(rag.get("top_k"), DEFAULT_RAG_TOP_K)
		self.similarity_threshold = _as_float(
			rag.get("similarity_threshold"), DEFAULT_RAG_SIMILARITY_THRESHOLD
		)

		self.context_window = _as_int(memory.get("context_window"), DEFAULT_MEMORY_CONTEXT_WINDOW)
		self.summary_model = _resolve_default(summary.get("model"), DEFAULT_MEMORY_SUMMARY_MODEL)
		self.summary_temperature = _as_float(
			summary.get("temperature"), DEFAULT_MEMORY_SUMMARY_TEMPERATURE
		)
		self.summary_max_tokens = _as_int(
			summary.get("max_tokens"), DEFAULT_MEMORY_SUMMARY_MAX_TOKENS
		)
		self.summary_reasoning_effort = _resolve_default(
			summary.get("reasoning_effort"), DEFAULT_MEMORY_SUMMARY_REASONING_EFFORT
		)
		self.api_max_retries = _as_int(api.get("max_retries"), DEFAULT_API_MAX_RETRIES)
		self.api_retry_base_delay = _as_float(
			api.get("retry_base_delay"), DEFAULT_API_RETRY_BASE_DELAY
		)
		self.api_retry_max_delay = _as_float(
			api.get("retry_max_delay"), DEFAULT_API_RETRY_MAX_DELAY
		)
		rag_db_path = Path(_resolve_default(rag.get("database"), DEFAULT_RAG_DATABASE))
		if not rag_db_path.is_absolute():
			rag_db_path = config_file.parent / rag_db_path
		self.rag_db_path = str(rag_db_path)
    