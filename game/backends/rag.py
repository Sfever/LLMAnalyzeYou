import json
import sqlite3
import threading
import time
from array import array
from pathlib import Path

import numpy as np
import openai
from config_reader import config


class rag:
    def __init__(self, config_path:str="config.json"):
        self.config = config(config_path)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.config.rag_db_path, check_same_thread=False)
        self.tag_list=[]
        self.skip_memory_point=True
        self._tag_set = set()
        with self._lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content TEXT NOT NULL,
                    metadata TEXT NOT NULL,
                    embedding BLOB NOT NULL,
                    embedding_norm REAL NOT NULL
                )"""
            )
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS memory_points (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    type TEXT NOT NULL,
                    key TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    content TEXT NOT NULL,
                    importance REAL NOT NULL,
                    confidence REAL NOT NULL
                )"""
            )
            self.conn.commit()
        self.embedding_client=openai.OpenAI(base_url="https://openrouter.ai/api/v1", api_key=self.config.api_key)
        self._ids: list[int] = []
        self._contents: list[str] = []
        self._metadata: list[str] = []
        self._embedding_matrix = np.empty((0, 0), dtype=np.float32)
        self._embedding_norms = np.empty(0, dtype=np.float32)
        self._load_embedding_cache()

    def close(self):
        with self._lock:
            if self.conn is not None:
                self.conn.close()
                self.conn = None

        close_embedding_client = getattr(self.embedding_client, "close", None)
        if callable(close_embedding_client):
            close_embedding_client()

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

    def add_document(self, content:str, metadata:dict):
        chunks=self._chunk_content(content)
        if not chunks:
            return

        embeddings = self._get_embeddings(chunks)
        pending_entries = []
        for sequence, (chunk, embedding_vector) in enumerate(zip(chunks, embeddings)):
            embedding=self._serialize_embedding(embedding_vector)
            chunk_metadata={
                "metadata": metadata,
                "sequence": sequence,
            }
            metadata_json = json.dumps(chunk_metadata)
            embedding_norm = self._vector_norm(embedding_vector)
            pending_entries.append(
                (
                    chunk,
                    metadata_json,
                    embedding,
                    np.asarray(embedding_vector, dtype=np.float32),
                    embedding_norm,
                )
            )
        self._insert_memory_entries(pending_entries)

    def add_memory(self, content:str, metadata:dict):
        embedding_vector=self._get_embedding(content)
        embedding=self._serialize_embedding(embedding_vector)
        metadata_json = json.dumps(metadata)
        embedding_norm = self._vector_norm(embedding_vector)
        self._insert_memory_entries([
            (
                content,
                metadata_json,
                embedding,
                np.asarray(embedding_vector, dtype=np.float32),
                embedding_norm,
            )
        ])

    def add_memories(self, contents:list[str], metadata_list:list[dict]):
        if len(contents)!=len(metadata_list):
            raise ValueError("Contents and metadata_list must have the same length.")
        for content, metadata in zip(contents, metadata_list):
            self.add_memory(content, metadata)

    def _chunk_document(self, content:str):
        step = max(1, self.config.chunk_size - self.config.chunk_overlap)
        return [content[i:i+self.config.chunk_size] for i in range(0, len(content), step)]

    def _chunk_content(self, content:str):
        strategy = str(self.config.chunking_strategy).strip().lower()
        if strategy == "llm":
            return self._llm_chunking(content)
        return self._chunk_document(content)
    
    def _llm_chunking(self, content:str):
        system_prompt=f"Chunk the following document into coherent sections of around {self.config.chunk_size} tokens each, with an overlap of about {self.config.chunk_overlap} tokens between chunks to preserve context. Try to split at natural boundaries like paragraphs or sections when possible. Document:\n{content}"
        json_schema={
            "name":"document_chunks",
            "strict":True,
            "schema":{
                "type":"array",
                "items":{
                    "type":"string",
                    "description":"A coherent chunk of the source document."
                }
            }
        }
        response = self._with_retry(
            lambda: self.embedding_client.chat.completions.create(
                model=self.config.summary_model,
                messages=[{"role":"system","content":system_prompt}],
                response_format={"type":"json_schema","json_schema":json_schema},
            ),
            action="document chunking",
        )
        return json.loads(response.choices[0].message.content)
    
    def _get_embedding(self, text:str):
        return self._get_embeddings([text])[0]

    def _get_embeddings(self, texts:list[str]):
        embeddings = []
        batch_size = max(1, self.config.embedding_batch_size)
        for index in range(0, len(texts), batch_size):
            batch = texts[index:index + batch_size]
            response = self._with_retry(
                lambda batch=batch: self.embedding_client.embeddings.create(
                    input=batch,
                    model=self.config.embedding_model,
                ),
                action="embedding generation",
            )
            embeddings.extend(item.embedding for item in response.data)
        return embeddings

    def _serialize_embedding(self, embedding:list[float]):
        packed = array("d", embedding)
        return packed.tobytes()

    def _deserialize_embedding(self, blob:bytes):
        return np.frombuffer(blob, dtype=np.float64).astype(np.float32, copy=True)

    def _vector_norm(self, vector:list[float]):
        return float(np.linalg.norm(np.asarray(vector, dtype=np.float32)))

    def _cosine_similarity(self, query_embedding, memory_embedding, query_norm:float, memory_norm:float):
        if query_norm == 0 or memory_norm == 0:
            return 0.0

        dot_product = sum(q * m for q, m in zip(query_embedding, memory_embedding))
        return dot_product / (query_norm * memory_norm)
    
    def _print_all_memories(self):
        with self._lock:
            rows = self.conn.execute("SELECT id, content, metadata FROM memories").fetchall()
        for row in rows:
            print(f"ID: {row[0]}, Content: {row[1]}, Metadata: {row[2]}")
    def _print_all_memory_points(self):
        with self._lock:
            rows = self.conn.execute("SELECT id, type, key, subject, content, importance, confidence FROM memory_points").fetchall()
        for row in rows:
            print(f"ID: {row[0]}, Type: {row[1]}, Key: {row[2]}, Subject: {row[3]}, Content: {row[4]}, Importance: {row[5]}, Confidence: {row[6]}")
    def _load_embedding_cache(self):
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, content, metadata, embedding, embedding_norm FROM memories ORDER BY id"
            ).fetchall()
            if not rows:
                self._ids = []
                self._contents = []
                self._metadata = []
                self._embedding_matrix = np.empty((0, 0), dtype=np.float32)
                self._embedding_norms = np.empty(0, dtype=np.float32)
                return

            embeddings = [self._deserialize_embedding(row[3]) for row in rows]
            self._ids = [row[0] for row in rows]
            self._contents = [row[1] for row in rows]
            self._metadata = [row[2] for row in rows]
            self._embedding_matrix = np.vstack(embeddings)
            self._embedding_norms = np.asarray([row[4] for row in rows], dtype=np.float32)

    def _insert_memory_entries(self, entries:list[tuple[str, str, bytes, np.ndarray, float]]):
        if not entries:
            return

        cache_entries = []
        with self._lock:
            for content, metadata_json, embedding, embedding_vector, embedding_norm in entries:
                cursor = self.conn.execute(
                    "INSERT INTO memories (content, metadata, embedding, embedding_norm) VALUES (?, ?, ?, ?)",
                    (content, metadata_json, embedding, embedding_norm),
                )
                cache_entries.append(
                    (
                        cursor.lastrowid,
                        content,
                        metadata_json,
                        embedding_vector,
                        embedding_norm,
                    )
                )
            self.conn.commit()
            self._append_cache_entries(cache_entries)

    def _append_cache_entries(self, entries:list[tuple[int, str, str, np.ndarray, float]]):
        if not entries:
            return

        self._ids.extend(entry[0] for entry in entries)
        self._contents.extend(entry[1] for entry in entries)
        self._metadata.extend(entry[2] for entry in entries)

        new_embeddings = np.vstack([entry[3] for entry in entries])
        new_norms = np.asarray([entry[4] for entry in entries], dtype=np.float32)
        if self._embedding_matrix.size == 0:
            self._embedding_matrix = new_embeddings
            self._embedding_norms = new_norms
            return

        self._embedding_matrix = np.vstack((self._embedding_matrix, new_embeddings))
        self._embedding_norms = np.concatenate((self._embedding_norms, new_norms))

    def close(self):
        with getattr(self, "_lock", threading.RLock()):
            if getattr(self, "conn", None) is None:
                return
            self.conn.close()
            self.conn = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def search(self, query:str, method:str="cosine"):
        if method=="cosine":
            return self._cosine_similarity_search(query)
        elif method=="model":
            return self._model_based_search(query)
        else:
            raise ValueError("Unsupported search method")
        
    def _add_recency_weight(self, candidate_indexes:np.ndarray, similarities:np.ndarray):
        candidate_similarities = similarities[candidate_indexes]
        candidate_ids = np.asarray([self._ids[index] for index in candidate_indexes], dtype=np.float32)

        if candidate_ids.size == 1:
            recency_scores = np.ones(1, dtype=np.float32)
        else:
            min_id = float(candidate_ids.min())
            max_id = float(candidate_ids.max())
            if max_id == min_id:
                recency_scores = np.ones(candidate_ids.shape, dtype=np.float32)
            else:
                recency_scores = (candidate_ids - min_id) / (max_id - min_id)

        return (0.85 * candidate_similarities) + (0.15 * recency_scores)
    
    def _add_overlap_weight(self, query:str, candidate_indexes:np.ndarray):
        query_words = [word for word in query.lower().split() if word]
        if not query_words:
            return np.zeros(candidate_indexes.shape, dtype=np.float32)

        query_word_set = set(query_words)
        query_word_count = float(len(query_word_set))
        overlap_scores = []
        for index in candidate_indexes:
            content_words = {word for word in self._contents[index].lower().split() if word}
            exact_overlap = len(query_word_set & content_words)
            overlap_scores.append(exact_overlap / query_word_count)

        return np.asarray(overlap_scores, dtype=np.float32)
    
    def _add_user_weight(self, candidate_indexes:np.ndarray):
        user_scores = []
        for index in candidate_indexes:
            try:
                metadata = json.loads(self._metadata[index])
            except (TypeError, json.JSONDecodeError):
                metadata = {}

            role = metadata.get("role")
            if role is None and isinstance(metadata.get("metadata"), dict):
                role = metadata["metadata"].get("role")

            user_scores.append(0.05 if role == "user" else 0.0)

        return np.asarray(user_scores, dtype=np.float32)

    def _cosine_similarity_search(self, query:str):
        query_embedding = np.asarray(self._get_embedding(query), dtype=np.float32)
        query_norm = self._vector_norm(query_embedding)
        if query_norm == 0:
            return []

        with self._lock:
            if self._embedding_matrix.size == 0:
                return []

            denominators = self._embedding_norms * query_norm
            valid = denominators > 0
            if not np.any(valid):
                return []

            similarities = np.zeros_like(denominators)
            similarities[valid] = self._embedding_matrix[valid] @ query_embedding / denominators[valid]
            candidate_indexes = np.flatnonzero(similarities >= self.config.similarity_threshold)
            if candidate_indexes.size == 0:
                return []

            weighted_scores = self._add_recency_weight(candidate_indexes, similarities)
            weighted_scores = weighted_scores + (0.1 * self._add_overlap_weight(query, candidate_indexes))
            weighted_scores = weighted_scores + self._add_user_weight(candidate_indexes)
            if self.config.top_k <= 0:
                ranked_indexes = candidate_indexes[np.argsort(weighted_scores)[::-1]]
                return [
                    (
                        self._ids[index],
                        self._contents[index],
                        self._metadata[index],
                        float(similarities[index]),
                    )
                    for index in ranked_indexes
                ]

            top_k = min(self.config.top_k, int(candidate_indexes.size))
            top_positions = np.argpartition(weighted_scores, -top_k)[-top_k:]
            ranked_indexes = candidate_indexes[top_positions]
            ranked_indexes = ranked_indexes[np.argsort(weighted_scores[top_positions])[::-1]]

            return [
                (
                    self._ids[index],
                    self._contents[index],
                    self._metadata[index],
                    float(similarities[index]),
                )
                for index in ranked_indexes
            ]
    
    def _model_based_search(self, query:str):
        pass

        


if __name__ == "__main__":
    instance=rag()
    test_document = Path(__file__).with_name("test.md").read_text(encoding="utf-8")
    instance._print_all_memory_points()
    instance._print_all_memories()

