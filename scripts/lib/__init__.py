"""Shared helpers for the build-time extraction scripts.

Modules here are imported by ``scripts/extract_*.py`` pipelines. They
are intentionally kept out of the runtime app (``screens/``,
``services/``) because they pull in heavy PDF / NLP dependencies that
end users shouldn't need just to take a mock.
"""
