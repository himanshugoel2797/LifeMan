"""Built-in output channels.

Each channel is a small class with a manifest + deliver/can_deliver/cancel.
Channels are registered into `lifeman.outputs.registry` at app startup.

The three Phase-1 channels per OUTPUT_DESIGN.MD are here:
- web_toast            (transient toast in the web UI)
- web_persistent       (sticky panel item until dismissed)
- digest               (accumulator; another tool reads this for periodic delivery)
"""
