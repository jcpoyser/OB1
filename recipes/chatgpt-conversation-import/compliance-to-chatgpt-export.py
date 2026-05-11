#!/usr/bin/env python3
"""
Convert ChatGPT Enterprise Compliance API JSON/JSONL exports into the
ChatGPT data-export shape expected by import-chatgpt.py.

OpenAI's public help docs describe the Compliance API at a high level, but the
endpoint schema is available only inside an Enterprise workspace. This converter
therefore accepts common JSON/JSONL export shapes and normalizes best-effort
message records into a synthetic conversations.json file.

Usage:
    python compliance-to-chatgpt-export.py /path/to/compliance-export.jsonl out-dir
    python compliance-to-chatgpt-export.py /path/to/export-folder out-dir

Then:
    python import-chatgpt.py out-dir --raw --include-trivial --max-words 0
"""

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path


CONVERSATION_ID_KEYS = (
    "conversation_id",
    "conversationId",
    "chatgpt_conversation_id",
    "chat_id",
    "chatId",
    "thread_id",
    "threadId",
    "session_id",
    "sessionId",
)

MESSAGE_LIST_KEYS = ("messages", "items", "events", "logs", "records")
TEXT_KEYS = ("text", "content", "message", "body", "prompt", "response", "output", "input")
TIME_KEYS = ("created_at", "create_time", "timestamp", "time", "event_time", "updated_at", "update_time")
TITLE_KEYS = ("title", "name", "subject", "conversation_title")
MODEL_KEYS = ("model", "model_slug", "default_model_slug")


def read_records(path):
    source = Path(path)
    if source.is_dir():
        records = []
        for child in sorted(source.rglob("*")):
            if child.suffix.lower() in {".json", ".jsonl", ".ndjson"}:
                records.extend(read_records(child))
        return records

    if source.suffix.lower() in {".jsonl", ".ndjson"}:
        out = []
        with source.open(encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out

    with source.open(encoding="utf-8-sig") as f:
        data = json.load(f)

    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("data", "results", "items", "records", "logs", "conversations"):
            value = data.get(key)
            if isinstance(value, list):
                return value
        return [data]
    return []


def first_value(obj, keys):
    if not isinstance(obj, dict):
        return None
    for key in keys:
        value = obj.get(key)
        if value not in (None, ""):
            return value
    return None


def nested_value(obj, dotted):
    cur = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def infer_conversation_id(record, parent=None):
    value = first_value(record, CONVERSATION_ID_KEYS)
    if value is None and parent:
        value = first_value(parent, CONVERSATION_ID_KEYS) or parent.get("id")
    if value is None:
        value = record.get("conversation", {}).get("id") if isinstance(record.get("conversation"), dict) else None
    if value is None:
        value = record.get("id")
    return str(value) if value not in (None, "") else "unknown-conversation"


def infer_title(record, parent=None, conv_id=""):
    value = first_value(record, TITLE_KEYS)
    if value is None and parent:
        value = first_value(parent, TITLE_KEYS)
    if value is None:
        value = record.get("conversation", {}).get("title") if isinstance(record.get("conversation"), dict) else None
    return str(value).strip() if value else f"Compliance export {conv_id}"


def infer_model(record, parent=None):
    value = first_value(record, MODEL_KEYS)
    if value is None and parent:
        value = first_value(parent, MODEL_KEYS)
    return str(value) if value else None


def parse_time(value):
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        # Compliance exports may use seconds or milliseconds.
        return value / 1000 if value > 10_000_000_000 else float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if re.fullmatch(r"\d+(\.\d+)?", text):
            return parse_time(float(text))
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def infer_time(record, parent=None):
    value = first_value(record, TIME_KEYS)
    if value is None and parent:
        value = first_value(parent, TIME_KEYS)
    return parse_time(value)


def text_from_content(value):
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        parts = []
        for item in value:
            text = text_from_content(item)
            if text:
                parts.append(text)
        return "\n".join(parts).strip()
    if isinstance(value, dict):
        for key in ("text", "value", "content", "body", "message"):
            text = text_from_content(value.get(key))
            if text:
                return text
        if isinstance(value.get("parts"), list):
            return text_from_content(value["parts"])
    return ""


def infer_text(record):
    for key in TEXT_KEYS:
        text = text_from_content(record.get(key))
        if text:
            return text

    for dotted in ("message.content", "message.text", "payload.content", "payload.text", "data.content", "data.text"):
        text = text_from_content(nested_value(record, dotted))
        if text:
            return text

    return ""


def infer_role(record):
    raw = (
        record.get("role")
        or nested_value(record, "author.role")
        or nested_value(record, "sender.role")
        or nested_value(record, "actor.role")
        or record.get("message_type")
        or record.get("type")
        or ""
    )
    role = str(raw).lower()
    if any(token in role for token in ("user", "human", "input", "prompt")):
        return "user"
    if any(token in role for token in ("assistant", "model", "gpt", "output", "response")):
        return "assistant"
    if "system" in role or "tool" in role or "processing" in role:
        return "system"
    return "assistant"


def iter_message_records(record, parent=None):
    if not isinstance(record, dict):
        return

    for key in MESSAGE_LIST_KEYS:
        value = record.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    yield from iter_message_records(item, parent=record)
            return

    text = infer_text(record)
    if text:
        yield record, parent


def make_message(record, parent=None):
    msg_id = str(record.get("id") or record.get("message_id") or record.get("item_id") or f"msg-{abs(hash(json.dumps(record, sort_keys=True, default=str)))}")
    create_time = infer_time(record, parent)
    return msg_id, {
        "id": msg_id,
        "author": {"role": infer_role(record)},
        "create_time": create_time,
        "update_time": create_time,
        "content": {
            "content_type": "text",
            "parts": [infer_text(record)],
        },
        "metadata": {
            "compliance_record_id": record.get("id") or record.get("message_id") or record.get("item_id"),
            "compliance_record_type": record.get("type") or record.get("message_type"),
        },
    }


def normalize(records):
    grouped = {}

    for record in records:
        for msg_record, parent in iter_message_records(record):
            conv_id = infer_conversation_id(msg_record, parent)
            bucket = grouped.setdefault(conv_id, {"parent": parent or record, "messages": []})
            bucket["messages"].append(make_message(msg_record, parent))

    conversations = []
    for conv_id, bucket in sorted(grouped.items()):
        messages = bucket["messages"]
        messages.sort(key=lambda pair: pair[1].get("create_time") or 0)

        parent = bucket["parent"] or {}
        title = infer_title(parent, conv_id=conv_id)
        model = infer_model(parent)
        times = [msg.get("create_time") for _, msg in messages if msg.get("create_time")]
        create_time = min(times) if times else datetime.now(timezone.utc).timestamp()
        update_time = max(times) if times else create_time

        mapping = {}
        prev_id = None
        for index, (msg_id, msg) in enumerate(messages):
            node_id = msg_id if msg_id not in mapping else f"{msg_id}-{index}"
            mapping[node_id] = {
                "id": node_id,
                "message": msg,
                "parent": prev_id,
                "children": [],
            }
            if prev_id:
                mapping[prev_id]["children"].append(node_id)
            prev_id = node_id

        conversations.append({
            "id": conv_id,
            "title": title,
            "create_time": create_time,
            "update_time": update_time,
            "current_node": prev_id,
            "mapping": mapping,
            "default_model_slug": model,
            "conversation_origin": "chatgpt_compliance_api",
        })

    return conversations


def main():
    parser = argparse.ArgumentParser(description="Convert Compliance API JSON/JSONL into ChatGPT export format")
    parser.add_argument("input_path", help="Compliance API export file or folder")
    parser.add_argument("output_dir", help="Directory where conversations.json will be written")
    args = parser.parse_args()

    records = read_records(args.input_path)
    conversations = normalize(records)

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "conversations.json"
    with out_path.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(conversations, f, ensure_ascii=False, indent=2)

    message_count = sum(len(c.get("mapping", {})) for c in conversations)
    print(f"Wrote {len(conversations)} conversations / {message_count} messages to {out_path}")
    if not conversations:
        print("No messages were detected. Provide a small redacted sample JSON object so the field mapping can be tightened.")


if __name__ == "__main__":
    main()
