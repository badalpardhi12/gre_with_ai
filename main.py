"""
Compatibility shim for ``python main.py``.

The real entry point is :mod:`app`. This file exists so that older docs,
external links, or muscle memory (``python main.py``) keep working after
the canonical entry point was renamed to ``app.py``.

Both invocations are equivalent:

    python app.py
    python main.py
"""

from app import main

if __name__ == "__main__":
    main()
