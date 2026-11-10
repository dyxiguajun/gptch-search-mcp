#!/bin/sh
# The Agent Plugins command itself must be bare or plugin-contained.
# Keep the original Python runtime outside the bundle in this local-only wrapper.
: "${GPTCH_PYTHON:?GPTCH_PYTHON must point to the existing gateway virtual environment}"
exec "$GPTCH_PYTHON" -m personal_search.server
