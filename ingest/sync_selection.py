#!/usr/bin/env python3
"""Shared attachment-selection helpers for incremental source parsing."""
import json
from pathlib import Path


def load_attachment_keys(path):
    """Return ``(event_id, attachment_index)`` keys from a sync manifest."""
    payload = json.loads(Path(path).read_text())
    entries = payload.get("attachments", payload) if isinstance(payload, dict) else payload
    return {
        (int(entry["eventId"]), int(entry["index"]))
        for entry in (entries or [])
    }


def select_jobs(jobs, event_id=None, manifest_path=None):
    """Select one event or only jobs newly discovered by the current sync."""
    if event_id is not None:
        return [job for job in jobs if job[0] == event_id]
    if manifest_path:
        keys = load_attachment_keys(manifest_path)
        return [job for job in jobs if (job[0], job[1]) in keys]
    return jobs
