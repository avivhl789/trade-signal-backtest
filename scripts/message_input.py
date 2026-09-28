# -*- coding: utf-8 -*-
"""Read the engine's platform-neutral message JSON format.

The engine deliberately does not collect messages.  Callers provide JSON files under
``data/raw`` (or another configured directory) after obtaining the data lawfully.

One file describes one logical stream::

    {
      "stream": {"id": "main", "name": "optional label"},
      "messages": [
        {
          "id": "unique-message-id",
          "timestamp": "2025-01-02T09:45:00+00:00",
          "content": "message text",
          "edited": "",
          "reply_to": "",
          "attachments": [{"name": "chart.png", "url": "..."}]
        }
      ]
    }

Only ``id``, ``timestamp`` and ``content`` are required on a message.  Input must
already contain only the messages the operator is authorised to analyse.
"""
import glob
import datetime as dt
import io
import json
import os


class InputFormatError(ValueError):
    pass


def _text(value):
    return "" if value is None else str(value)


def _timestamp(value, path, index):
    raw = _text(value).strip()
    probe = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = dt.datetime.fromisoformat(probe)
    except ValueError:
        raise InputFormatError("%s: messages[%d].timestamp is not ISO 8601" %
                               (path, index))
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        raise InputFormatError("%s: messages[%d].timestamp must be UTC (Z or +00:00)" %
                               (path, index))
    return raw


def _attachments(value, path, index):
    if value is None:
        return []
    if not isinstance(value, list):
        raise InputFormatError("%s: messages[%d].attachments must be a list" %
                               (path, index))
    out = []
    for j, item in enumerate(value):
        if not isinstance(item, dict):
            raise InputFormatError(
                "%s: messages[%d].attachments[%d] must be an object" %
                (path, index, j))
        out.append({"name": _text(item.get("name")), "url": _text(item.get("url"))})
    return out


def load(root, streams=None):
    """Return normalized messages from every JSON file below ``root``.

    ``streams`` optionally limits input to a set of logical stream ids. Message ids
    must be unique across the selected files. Exact duplicate rows are ignored; a
    conflicting duplicate is rejected instead of selecting one by filename order.
    """
    wanted = None if streams is None else frozenset(str(x) for x in streams if x)
    rows, seen = [], {}
    pattern = os.path.join(root, "**", "*.json")
    for path in sorted(glob.glob(pattern, recursive=True)):
        try:
            with io.open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError) as exc:
            raise InputFormatError("%s: cannot read JSON: %s" % (path, exc))
        if not isinstance(doc, dict):
            raise InputFormatError("%s: top level must be an object" % path)
        stream = doc.get("stream")
        if not isinstance(stream, dict) or not _text(stream.get("id")).strip():
            raise InputFormatError("%s: stream.id is required" % path)
        stream_id = _text(stream["id"]).strip()
        if wanted is not None and stream_id not in wanted:
            continue
        messages = doc.get("messages")
        if not isinstance(messages, list):
            raise InputFormatError("%s: messages must be a list" % path)
        for i, item in enumerate(messages):
            if not isinstance(item, dict):
                raise InputFormatError("%s: messages[%d] must be an object" % (path, i))
            missing = [key for key in ("id", "timestamp", "content") if key not in item]
            if missing:
                raise InputFormatError("%s: messages[%d] missing %s" %
                                       (path, i, ", ".join(missing)))
            row = {
                "id": _text(item["id"]).strip(),
                "timestamp": _timestamp(item["timestamp"], path, i),
                "edited": _text(item.get("edited")).strip(),
                "stream_id": stream_id,
                "stream_name": _text(stream.get("name")).strip(),
                "content": _text(item["content"]),
                "reply_to": _text(item.get("reply_to")).strip(),
                "attachments": _attachments(item.get("attachments"), path, i),
            }
            if not row["id"] or not row["timestamp"]:
                raise InputFormatError("%s: messages[%d] id/timestamp cannot be empty" %
                                       (path, i))
            prior = seen.get(row["id"])
            if prior is not None:
                if prior != row:
                    raise InputFormatError("%s: conflicting duplicate message id %s" %
                                           (path, row["id"]))
                continue
            seen[row["id"]] = row
            rows.append(row)
    rows.sort(key=lambda row: (row["timestamp"], row["id"]))
    return rows
