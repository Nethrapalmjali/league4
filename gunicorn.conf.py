"""Gunicorn settings, read automatically from this folder by `gunicorn app:app`.

Before this file the server ran gunicorn's default: one worker answering one
request at a time. Every spectator's scoreboard asks for new scores every few
seconds, so a scorer's tap had to wait in line behind all of them. Threads let
those wait on the database side by side; the database pool (app.py) has the
same number of connections.
"""
import os

worker_class = "gthread"
workers = int(os.getenv("WEB_CONCURRENCY", "1"))
threads = int(os.getenv("GUNICORN_THREADS", "8"))
timeout = 120
graceful_timeout = 30
keepalive = 5
