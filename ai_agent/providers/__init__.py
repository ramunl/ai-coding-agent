"""Per-provider capability modules discovered by ai_agent.provider_registry.

Each module declares one provider's role support, adapters, model selection,
auth, limits, error classification, image and structured-output support,
usage counting and enabled state. Import the submodules directly or through
``ai_agent.provider_registry.discover()``; this package imports nothing by
default so discovery stays lazy and side-effect free.
"""
