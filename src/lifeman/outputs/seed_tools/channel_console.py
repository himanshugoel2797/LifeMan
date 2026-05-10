"""Example output channel tool — logs delivered events to the core log.

Useful as a template when building real channels (push, email, SMS, etc.).
The manifest fragment to register this tool with should look like:

    {
      "name": "console",
      "description": "Logs delivered output events.",
      "manifest": {
        "role": "output_channel",
        "output_channel": {
          "channel_type": "log",
          "capabilities": {
            "rich_content": false, "images": false,
            "actions": false, "persistence": false,
            "interruption_level": "background", "typical_latency_ms": 1
          },
          "sensitivity_tolerance": "private"
        }
      },
      "code": "<contents of this file>"
    }

Tool I/O (defined in lifeman.outputs.tool_backed):
    input  = {method, event?, output_id?, delivery_id?}
    output = depends on method
"""

import json
import sys

import lifeman_tool


def deliver(event):
    content = event.get("content")
    if isinstance(content, dict):
        text = content.get("title") or content.get("body") or ""
    else:
        text = str(content)
    lifeman_tool.log(f"console deliver {event.get('output_id')}: {text[:200]}")
    return {"delivered": True, "delivery_id": event.get("output_id")}


def can_deliver(_event):
    return {"ok": True}


def cancel(output_id, _delivery_id):
    lifeman_tool.log(f"console cancel {output_id}")
    return {"ok": True}


def main():
    payload = json.loads(sys.stdin.read())
    method = payload.get("method")
    if method == "deliver":
        out = deliver(payload["event"])
    elif method == "can_deliver":
        out = can_deliver(payload["event"])
    elif method == "cancel":
        out = cancel(payload.get("output_id", ""), payload.get("delivery_id"))
    else:
        out = {"error": f"unknown method {method!r}"}
    sys.stdout.write(json.dumps(out))


if __name__ == "__main__":
    main()
