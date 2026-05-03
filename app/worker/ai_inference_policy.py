"""
Project inference policy:

Remote third-party inference APIs (OpenAI, Google Gemini, etc.) are DISABLED.

Use only local rules + ONNX/embeddings bundled with the repo (post_classifier).
Change REMOTE_CLOUD_AI_ENABLED only if your deployment explicitly allows it.
"""

REMOTE_CLOUD_AI_ENABLED: bool = False
