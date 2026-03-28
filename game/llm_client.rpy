init python:
    import copy
    import json
    import os
    import sqlite3
    import subprocess
    import sys
    import threading
    import time

    store = renpy.store
    _llm_pending_rag_restore_slot = None

    _LLM_PAGE_CHAR_LIMIT = 200
    _LLM_DEBUG_LOG_LIMIT = 80


    def _llm_ensure_store_defaults():
        defaults = {
            "llm_chat_history": [],
            "llm_live_text": "",
            "llm_stream_done": False,
            "llm_stream_active": False,
            "llm_stream_close_ready": False,
            "llm_stream_error": "",
            "llm_page_texts": [],
            "llm_current_page": 0,
            "llm_backend_python": "python",
            "llm_backend_max_tool_rounds": 4,
            "llm_verbose_logging": True,
            "llm_debug_lines": [],
            "llm_request_state": "",
        }
        for name, value in defaults.items():
            if not hasattr(store, name):
                setattr(store, name, copy.deepcopy(value))


    def _llm_get(name, default=None):
        if not hasattr(store, name):
            if default is None:
                return None
            value = copy.deepcopy(default)
            setattr(store, name, value)
            return value
        return getattr(store, name)


    def _llm_set(name, value):
        if threading.current_thread() is threading.main_thread():
            setattr(store, name, value)
            return value

        done = threading.Event()
        error_box = {"error": None}
 
        def apply_value():
            try:
                setattr(store, name, value)
            except Exception as error:
                error_box["error"] = error
            finally:
                done.set()

        renpy.invoke_in_main_thread(apply_value)
        done.wait()

        if error_box["error"] is not None:
            raise error_box["error"]
        return value


    _llm_ensure_store_defaults()


    def _llm_backends_dir():
        return os.path.join(config.gamedir, "backends")


    def _llm_backend_script_path():
        return os.path.join(_llm_backends_dir(), "backend.py")


    def _llm_backend_config_path():
        return os.path.join(_llm_backends_dir(), "config.json")


    def _llm_rag_db_path():
        config_path = _llm_backend_config_path()
        database_name = "rag.db"

        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as config_file:
                    config_data = json.load(config_file)
                database_name = ((config_data.get("rag") or {}).get("database") or database_name)
            except Exception:
                pass

        if os.path.isabs(database_name):
            return os.path.normpath(database_name)

        return os.path.normpath(os.path.join(os.path.dirname(config_path), database_name))


    def _llm_rag_snapshot_dir():
        return os.path.join(config.savedir, "rag_snapshots")


    def _llm_safe_slot_name(slot_name):
        safe_name = []
        for character in str(slot_name or ""):
            if character.isalnum() or character in ("-", "_", "."):
                safe_name.append(character)
            else:
                safe_name.append("_")
        return "".join(safe_name) or "slot"


    def _llm_rag_snapshot_path(slot_name):
        return os.path.join(_llm_rag_snapshot_dir(), _llm_safe_slot_name(slot_name) + ".sqlite3")


    def _llm_slot_name(name, page=None, slot=False):
        if slot:
            return str(name)

        page_name = page if page is not None else renpy.current_screen().scope.get("page_name_value", None)
        if page_name is None:
            page_name = FileCurrentPage()

        if config.file_slotname_callback is not None:
            return str(config.file_slotname_callback(page_name, name))

        return "%s-%s" % (page_name, name)


    def _llm_sqlite_backup(source_path, target_path):
        source_connection = None
        target_connection = None
        temp_path = target_path + ".tmp"

        target_dir = os.path.dirname(target_path)
        if target_dir and not os.path.isdir(target_dir):
            os.makedirs(target_dir)

        for candidate in (temp_path,):
            if os.path.exists(candidate):
                os.unlink(candidate)

        try:
            source_connection = sqlite3.connect(source_path)
            try:
                source_connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except Exception:
                pass

            target_connection = sqlite3.connect(temp_path)
            source_connection.backup(target_connection)
        finally:
            if target_connection is not None:
                target_connection.close()
            if source_connection is not None:
                source_connection.close()

        os.replace(temp_path, target_path)


    def _llm_snapshot_rag_for_slot(slot_name):
        source_path = _llm_rag_db_path()
        snapshot_path = _llm_rag_snapshot_path(slot_name)

        if not os.path.exists(source_path):
            _llm_log("Skipping RAG snapshot for %s because %s does not exist." % (slot_name, source_path))
            return False

        try:
            _llm_sqlite_backup(source_path, snapshot_path)
            _llm_log("Saved RAG snapshot for slot %s." % slot_name)
            return True
        except Exception as error:
            _llm_log("Failed to save RAG snapshot for %s: %s" % (slot_name, _llm_preview_text(error, 220)))
            return False


    def _llm_restore_rag_from_slot(slot_name):
        snapshot_path = _llm_rag_snapshot_path(slot_name)
        target_path = _llm_rag_db_path()

        if not os.path.exists(snapshot_path):
            _llm_log("No RAG snapshot found for slot %s." % slot_name)
            return False

        target_dir = os.path.dirname(target_path)
        if target_dir and not os.path.isdir(target_dir):
            os.makedirs(target_dir)

        for suffix in ("-wal", "-shm", "-journal"):
            candidate = target_path + suffix
            if os.path.exists(candidate):
                try:
                    os.unlink(candidate)
                except Exception:
                    pass

        try:
            _llm_sqlite_backup(snapshot_path, target_path)
            _llm_log("Restored RAG snapshot from slot %s." % slot_name)
            return True
        except Exception as error:
            _llm_log("Failed to restore RAG snapshot for %s: %s" % (slot_name, _llm_preview_text(error, 220)))
            return False


    def _llm_schedule_rag_restore(slot_name):
        global _llm_pending_rag_restore_slot
        _llm_pending_rag_restore_slot = slot_name
        _llm_log("Scheduled RAG restore for slot %s." % slot_name)


    def _llm_restore_pending_rag_snapshot():
        global _llm_pending_rag_restore_slot
        slot_name = _llm_pending_rag_restore_slot
        _llm_pending_rag_restore_slot = None

        if not slot_name:
            return

        _llm_restore_rag_from_slot(slot_name)


    def _llm_snapshot_latest_autosave():
        slot_name = renpy.newest_slot(r"auto-")
        if slot_name:
            _llm_snapshot_rag_for_slot(slot_name)


    def llm_file_save_action(name, page=None, confirm=True, newest=True, cycle=False, slot=False):
        slot_name = _llm_slot_name(name, page=page, slot=slot)
        return FileSave(
            name,
            confirm=confirm,
            newest=newest,
            page=page,
            cycle=cycle,
            slot=slot,
            action=Function(_llm_snapshot_rag_for_slot, slot_name),
        )


    def llm_file_load_action(name, page=None, confirm=True, newest=True, cycle=False, slot=False):
        slot_name = _llm_slot_name(name, page=page, slot=slot)
        load_action = FileLoad(
            name,
            confirm=False,
            newest=newest,
            page=page,
            cycle=cycle,
            slot=slot,
        )

        if confirm and not main_menu:
            return Confirm(
                _("Load save?"),
                [Function(_llm_schedule_rag_restore, slot_name), load_action],
                None,
            )

        return [Function(_llm_schedule_rag_restore, slot_name), load_action]


    def _llm_write_backend_config():
        config_path = _llm_backend_config_path()
        config_data = {}

        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as config_file:
                    config_data = json.load(config_file)
            except Exception:
                config_data = {}

        chat_config = dict(config_data.get("chat", {}))
        rag_config = dict(config_data.get("rag", {}))
        memory_config = dict(config_data.get("memory", {}))
        summary_config = dict(memory_config.get("summary", {}))

        config_data["api_key"] = store.llm_api_key
        chat_config["model"] = store.llm_model or "default"
        rag_config["embedding_model"] = store.llm_embedding_model or rag_config.get("embedding_model", "default")
        summary_config["model"] = store.llm_summarize_model or "default"

        memory_config["summary"] = summary_config
        config_data["chat"] = chat_config
        config_data["rag"] = rag_config
        config_data["memory"] = memory_config

        with open(config_path, "w", encoding="utf-8") as config_file:
            json.dump(config_data, config_file, ensure_ascii=False, indent=4)

        return config_path


    config.after_load_callbacks.append(_llm_restore_pending_rag_snapshot)
    config.autosave_callback = _llm_snapshot_latest_autosave


    def _llm_backend_command():
        python_executable = _llm_get("llm_backend_python", "python") or sys.executable
        return [python_executable, _llm_backend_script_path(), "--config", _llm_backend_config_path()]


    def _llm_creation_flags():
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)


    def _llm_verbose_log_path():
        return os.path.join(config.gamedir, "cache", "llm_verbose.log")


    def _llm_preview_text(text, limit=160):
        text = str(text or "").replace("\r", " ").replace("\n", " ").strip()
        if len(text) <= limit:
            return text
        return text[: limit - 3] + "..."


    def _llm_log(message):
        if not _llm_get("llm_verbose_logging", True):
            return

        timestamp = time.strftime("%H:%M:%S")
        line = "[%s] %s" % (timestamp, message)
        debug_lines = list(_llm_get("llm_debug_lines", []))
        debug_lines.append(line)
        if len(debug_lines) > _LLM_DEBUG_LOG_LIMIT:
            debug_lines = debug_lines[-_LLM_DEBUG_LOG_LIMIT:]
        _llm_set("llm_debug_lines", debug_lines)

        try:
            log_path = _llm_verbose_log_path()
            log_dir = os.path.dirname(log_path)
            if log_dir and not os.path.isdir(log_dir):
                os.makedirs(log_dir)
            with open(log_path, "a", encoding="utf-8") as log_file:
                log_file.write(line + "\n")
        except Exception:
            pass


    def llm_recent_debug_text(limit=8):
        debug_lines = _llm_get("llm_debug_lines", [])
        if not debug_lines:
            return ""
        return "\n".join(debug_lines[-max(1, limit):])


    def llm_request_status_text():
        page_texts = _llm_get("llm_page_texts", [])
        stream_done = _llm_get("llm_stream_done", False)
        if stream_done and len(page_texts) > 1:
            current_page = min(_llm_get("llm_current_page", 0), len(page_texts) - 1) + 1
            if current_page < len(page_texts):
                return "Page %d/%d. Press space or click for next page." % (current_page, len(page_texts))
            return "Page %d/%d. Press space or click to continue." % (current_page, len(page_texts))

        request_state = _llm_get("llm_request_state", "")
        if request_state:
            return request_state
        if _llm_get("llm_stream_error", ""):
            return "Request failed."
        if _llm_get("llm_stream_active", False):
            return "Request in progress..."
        if _llm_get("llm_stream_done", False):
            return "Reply ready. Click to continue."
        return ""


    def _llm_paginate_text(text, max_chars=_LLM_PAGE_CHAR_LIMIT):
        text = text or ""
        if not text:
            return [""]

        pages = []
        remaining = text.strip()
        while remaining:
            if len(remaining) <= max_chars:
                pages.append(remaining)
                break

            split_at = remaining.rfind("\n", 0, max_chars)
            if split_at <= 0:
                split_at = remaining.rfind(" ", 0, max_chars)
            if split_at <= 0:
                split_at = max_chars

            pages.append(remaining[:split_at].rstrip())
            remaining = remaining[split_at:].lstrip()

        return pages or [""]


    def _llm_reset_stream_state():
        _llm_set("llm_live_text", "")
        _llm_set("llm_stream_done", False)
        _llm_set("llm_stream_active", False)
        _llm_set("llm_stream_close_ready", False)
        _llm_set("llm_stream_error", "")
        _llm_set("llm_page_texts", [])
        _llm_set("llm_current_page", 0)
        _llm_set("llm_debug_lines", [])
        _llm_set("llm_request_state", "Preparing request...")


    def llm_clear_history():
        _llm_set("llm_chat_history", [])

    def llm_get_history():
        return copy.deepcopy(_llm_get("llm_chat_history", []))


    def llm_add_message(role, content=None, **extra_fields):
        message = {"role": role}
        if content is not None:
            message["content"] = content
        for key, value in extra_fields.items():
            message[key] = value
        history = _llm_get("llm_chat_history", [])
        history.append(message)
        return message


    def llm_add_system_message(content):
        return llm_add_message("system", content)


    def llm_add_user_message(content):
        return llm_add_message("user", content)


    def llm_copy_history():
        return copy.deepcopy(_llm_get("llm_chat_history", []))


    def _llm_resolve_history_character(speaker):
        try:
            add_history = object.__getattribute__(speaker, "add_history")
        except Exception:
            add_history = None

        if callable(add_history):
            return speaker

        if isinstance(speaker, str):
            name_only = getattr(renpy.store, "name_only", None)
            if name_only is not None:
                return name_only

        return getattr(renpy.store, "adv", None)


    def _llm_add_dialogue_history_entry(speaker, text):
        text = (text or "").strip()
        if not text:
            return

        if config.history_length is None or not getattr(renpy.store, "_history", True):
            return

        history_character = _llm_resolve_history_character(speaker)
        if history_character is None or not hasattr(history_character, "add_history"):
            _llm_log("Unable to resolve a history character for %r." % (speaker,))
            return

        speaker_name = speaker if isinstance(speaker, str) else str(speaker)

        try:
            history_character.add_history("adv", speaker_name, text)
            _llm_log("Added streamed reply to dialogue history.")
        except Exception as error:
            _llm_log("Failed to add dialogue history entry: %s" % _llm_preview_text(error, 220))


    def _llm_ensure_assistant_message_in_chat_history(text):
        text = (text or "").strip()
        if not text:
            return

        history = copy.deepcopy(_llm_get("llm_chat_history", []))
        if history:
            last_message = history[-1]
            if last_message.get("role") == "assistant" and (last_message.get("content") or "").strip() == text:
                return

        history.append({
            "role": "assistant",
            "content": text,
        })
        _llm_set("llm_chat_history", history)
        _llm_log("Recovered missing assistant turn in llm_chat_history.")


    _LLM_SMART_PUNCT = [
        ("\u2018", "'"), ("\u2019", "'"),
        ("\u201C", '"'), ("\u201D", '"'),
        ("\u2013", "-"), ("\u2014", "--"),
        ("\u2026", "..."),
    ]

    def _llm_normalize_text(text):
        for fancy, plain in _LLM_SMART_PUNCT:
            text = text.replace(fancy, plain)
        return text

    def llm_escape_text(text):
        text = _llm_normalize_text(text or "")
        return text.replace("{", "{{").replace("}", "}}")


    def llm_visible_text():
        stream_done = _llm_get("llm_stream_done", False)
        page_texts = _llm_get("llm_page_texts", [])
        if stream_done and page_texts:
            page_index = min(_llm_get("llm_current_page", 0), len(page_texts) - 1)
            return page_texts[page_index]
        return _llm_get("llm_live_text", "")


    def llm_page_status_text():
        stream_done = _llm_get("llm_stream_done", False)
        page_texts = _llm_get("llm_page_texts", [])
        if not stream_done or len(page_texts) <= 1:
            return ""
        return "Page %d/%d" % (_llm_get("llm_current_page", 0) + 1, len(page_texts))


    def _llm_read_pipe(pipe, sink, label):
        try:
            for raw_line in pipe:
                line = raw_line.rstrip("\r\n")
                sink.append(line)
                if line:
                    _llm_log("%s: %s" % (label, _llm_preview_text(line, 220)))
        finally:
            pipe.close()


    def _llm_run_on_main_thread(function, *args, **kwargs):
        done = threading.Event()
        result_box = {"value": None, "error": None}

        def invoke():
            try:
                result_box["value"] = function(*args, **kwargs)
            except Exception as error:
                result_box["error"] = error
            finally:
                done.set()

        renpy.invoke_in_main_thread(invoke)
        done.wait()

        if result_box["error"] is not None:
            raise result_box["error"]
        return result_box["value"]


    def _llm_change_expression(character, expression):
        image_name = "%s %s" % (character, expression)
        renpy.show(image_name, tag=character)
        renpy.restart_interaction()
        return {
            "ok": True,
            "character": character,
            "expression": expression,
        }


    def _llm_execute_tool_call(tool_call):
        tool_name = tool_call.get("name")
        arguments = tool_call.get("arguments") or {}

        if tool_name != "change_expression":
            return {
                "ok": False,
                "error": "Unsupported tool: %s" % tool_name,
                "next_step": "Continue the conversation with the user in your next assistant message.",
            }

        character = str(arguments.get("character", "")).strip()
        expression = str(arguments.get("expression", "")).strip()
        if not character or not expression:
            return {
                "ok": False,
                "error": "Tool call requires character and expression.",
                "next_step": "Continue the conversation with the user in your next assistant message.",
            }

        tool_result = _llm_run_on_main_thread(_llm_change_expression, character, expression)
        tool_result["next_step"] = "Continue the conversation with the user in your next assistant message."
        return tool_result


    def _llm_append_continuation_prompt(messages, tool_names=None):
        tool_names = [name for name in (tool_names or []) if name]
        if tool_names:
            tool_summary = ", ".join(tool_names)
            content = (
                "The requested tool call(s) completed successfully: %s. "
                "Now send the assistant's natural in-character reply to the user. "
                "Do not stop at tool output, and do not repeat the raw tool result unless the user needs it."
            ) % tool_summary
        else:
            content = (
                "The requested tool has finished successfully. Continue the conversation with a natural in-character reply to the user."
            )

        messages.append(
            {
                "role": "system",
                "content": content,
            }
        )


    def _llm_tool_call_message(tool_call):
        tool_call_id = tool_call.get("id") or ("tool_call_%s" % tool_call.get("name", "unknown"))
        return {
            "id": tool_call_id,
            "type": "function",
            "function": {
                "name": tool_call.get("name"),
                "arguments": json.dumps(tool_call.get("arguments") or {}, ensure_ascii=False),
            },
        }


    def _llm_append_assistant_turn(messages, content, tool_calls):
        assistant_message = {"role": "assistant"}

        if content:
            assistant_message["content"] = content
        if tool_calls:
            assistant_message["tool_calls"] = [_llm_tool_call_message(tool_call) for tool_call in tool_calls]

        if "content" not in assistant_message and "tool_calls" not in assistant_message:
            assistant_message["content"] = ""

        messages.append(assistant_message)


    def _llm_spawn_backend():
        _llm_write_backend_config()
        command = _llm_backend_command()
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        _llm_log("Spawning backend: %s" % " ".join(command))
        return subprocess.Popen(
            command,
            cwd=_llm_backends_dir(),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=_llm_creation_flags(),
        )


    def _llm_run_single_request(messages):
        process = _llm_spawn_backend()
        stderr_lines = []
        stderr_thread = threading.Thread(
            target=_llm_read_pipe,
            args=(process.stderr, stderr_lines, "backend stderr"),
            daemon=True,
        )
        stderr_thread.start()

        try:
            payload = json.dumps(messages, ensure_ascii=False) + "\n"
            _llm_set("llm_request_state", "Sending request to backend...")
            _llm_log(
                "Sending %d messages. Last message: %s" % (
                    len(messages),
                    _llm_preview_text((messages[-1] or {}).get("content", "")) if messages else "<none>",
                )
            )
            process.stdin.write(payload)
            process.stdin.flush()
            process.stdin.close()

            final_event = None
            tool_calls = []
            chunk_count = 0

            for raw_line in process.stdout:
                line = raw_line.strip()
                if not line:
                    continue

                event = json.loads(line)
                event_type = event.get("type")
                _llm_log("Received event: %s" % event_type)

                if event_type == "chunk":
                    chunk_text = event.get("content", "")
                    chunk_count += 1
                    if chunk_count == 1:
                        _llm_set("llm_request_state", "Request in progress. Receiving reply...")
                    _llm_log("Chunk %d: %s" % (chunk_count, _llm_preview_text(chunk_text)))
                    _llm_set("llm_live_text", _llm_get("llm_live_text", "") + chunk_text)
                elif event_type == "tool_call":
                    tool_call = event.get("tool_call") or {}
                    if not tool_call.get("id"):
                        tool_call["id"] = "tool_call_%d" % (len(tool_calls) + 1)
                    tool_calls.append(tool_call)
                    _llm_set("llm_request_state", "Request in progress. Executing tool call...")
                    _llm_log(
                        "Tool call queued: %s %s" % (
                            tool_call.get("name") or "<unknown>",
                            _llm_preview_text(json.dumps(tool_call.get("arguments") or {}, ensure_ascii=False)),
                        )
                    )
                elif event_type == "done":
                    final_event = event
                    _llm_set("llm_request_state", "Finalizing reply...")
                    break
                elif event_type == "error":
                    raise RuntimeError(event.get("error") or "Backend error.")

            process.stdout.close()
            return_code = process.wait()
            stderr_thread.join(timeout=1.0)
            _llm_log("Backend exited with code %s." % return_code)

            if final_event is None:
                stderr_output = "\n".join(stderr_lines).strip()
                if stderr_output:
                    raise RuntimeError(stderr_output)
                raise RuntimeError("Backend exited without a completion event.")

            if return_code not in (0, None):
                stderr_output = "\n".join(stderr_lines).strip()
                if stderr_output:
                    raise RuntimeError(stderr_output)
                raise RuntimeError("Backend exited with code %d." % return_code)

            message = final_event.get("message") or {}
            content = message.get("content", "")
            if content:
                _llm_set("llm_live_text", content)
            _llm_log("Final content length: %d" % len(content or ""))

            final_tool_calls = final_event.get("tool_calls")
            if isinstance(final_tool_calls, list):
                tool_calls = final_tool_calls
                if tool_calls:
                    _llm_log("Final tool call count: %d" % len(tool_calls))

            return {
                "content": content,
                "tool_calls": tool_calls,
            }
        finally:
            try:
                process.stdout.close()
            except Exception:
                pass
            try:
                process.stdin.close()
            except Exception:
                pass
            if process.poll() is None:
                process.kill()
                process.wait()


    def _run_llm_stream(messages):
        working_messages = copy.deepcopy(messages)
        tool_rounds = 0
        continuation_retries = 0

        try:
            while True:
                _llm_log("Starting request round %d." % (tool_rounds + 1))
                result = _llm_run_single_request(working_messages)
                content = result.get("content", "")
                tool_calls = result.get("tool_calls") or []

                if tool_rounds > 0 and not tool_calls and not (content or "").strip():
                    if continuation_retries >= 1:
                        raise RuntimeError("The model finished after tool use without a follow-up reply.")
                    continuation_retries += 1
                    _llm_append_continuation_prompt(working_messages)
                    continue

                continuation_retries = 0

                _llm_append_assistant_turn(working_messages, content, tool_calls)

                if not tool_calls:
                    _llm_set("llm_chat_history", copy.deepcopy(working_messages))
                    _llm_set("llm_request_state", "Reply ready. Click to continue.")
                    _llm_log("Request completed without pending tool calls.")
                    break

                tool_rounds += 1
                if tool_rounds > _llm_get("llm_backend_max_tool_rounds", 4):
                    raise RuntimeError("Exceeded tool-call continuation limit.")

                for tool_call in tool_calls:
                    tool_call_id = tool_call.get("id") or ("tool_call_%d" % tool_rounds)
                    _llm_log("Executing tool call %s." % (tool_call.get("name") or "<unknown>"))
                    tool_result = _llm_execute_tool_call(tool_call)
                    _llm_log(
                        "Tool result %s: %s" % (
                            tool_call.get("name") or "<unknown>",
                            _llm_preview_text(json.dumps(tool_result, ensure_ascii=False), 220),
                        )
                    )
                    working_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "content": json.dumps(tool_result, ensure_ascii=False),
                        }
                    )

                _llm_set("llm_request_state", "Tool call finished. Requesting follow-up reply...")
                _llm_append_continuation_prompt(
                    working_messages,
                    [tool_call.get("name") for tool_call in tool_calls],
                )

                _llm_set("llm_chat_history", copy.deepcopy(working_messages))

        except Exception as error:
            _llm_log("Request failed: %s" % _llm_preview_text(error, 220))
            _llm_set("llm_request_state", "Request failed. Check verbose log.")
            _llm_set("llm_stream_error", str(error))
        finally:
            _llm_set("llm_page_texts", _llm_paginate_text(_llm_get("llm_live_text", "")))
            _llm_set("llm_current_page", 0)
            _llm_set("llm_stream_active", False)
            _llm_set("llm_stream_done", True)
            _llm_log("Stream finished. Visible text length: %d" % len(_llm_get("llm_live_text", "") or ""))


    def _begin_llm_stream(messages=None):
        if _llm_get("llm_stream_active", False):
            raise RuntimeError("LLM stream is already active.")

        _llm_set("llm_verbose_logging", True)
        _llm_reset_stream_state()
        _llm_set("llm_stream_active", True)
        _llm_set("llm_request_state", "Launching backend...")

        working_messages = copy.deepcopy(messages if messages is not None else _llm_get("llm_chat_history", []))
        _llm_log("Beginning stream with %d messages." % len(working_messages))
        worker = threading.Thread(target=_run_llm_stream, args=(working_messages,), daemon=True)
        worker.start()
        return worker


    def llm_stream_reply(speaker_name="Muse", messages=None):
        _begin_llm_stream(messages)
        _llm_log("Opening llm_stream_dialogue screen.")
        renpy.call_screen("llm_stream_dialogue", speaker_name)
        renpy.hide_screen("llm_stream_dialogue")

        if not _llm_get("llm_stream_error", ""):
            final_text = _llm_get("llm_live_text", "")
            _llm_ensure_assistant_message_in_chat_history(final_text)
            _llm_add_dialogue_history_entry(speaker_name, final_text)

        renpy.restart_interaction()
        return copy.deepcopy(_llm_get("llm_chat_history", []))