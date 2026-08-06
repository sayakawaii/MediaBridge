"""Makes the suite a package so shared helpers survive a bare ``pytest``.

``test_horizon`` imports ``ALLOWED_TAGS``/``tags_in`` from ``test_acfun_html``.
Without this file pytest imports each module top-level and only ``python -m
pytest`` resolves ``tests.`` -- because ``-m`` puts the working directory on
``sys.path`` and a bare ``pytest`` does not.
"""
