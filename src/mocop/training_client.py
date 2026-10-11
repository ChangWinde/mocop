"""Bounded owner-scoped GETs with a private token and no redirect forwarding."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import deque
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .privatefiles import is_private_regular_file
from .training_config import TrainingJobsConfig
from .training_projection import ReadError


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class TrainingClient:
    def __init__(self, config: TrainingJobsConfig):
        self.config = config
        self.lock = threading.Lock()
        self.requests = deque()
        self.cooldown_until = 0.0
        self.connections = threading.BoundedSemaphore(2)

    def token(self):
        if self.config.token_file is None:
            raise ReadError(401)
        try:
            descriptor = os.open(
                self.config.token_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            )
            with os.fdopen(descriptor, "rb") as stream:
                if not is_private_regular_file(os.fstat(stream.fileno())):
                    raise ReadError(401)
                raw = stream.read(257)
                if len(raw) > 256:
                    raise ReadError(401)
                value = raw.decode("ascii").strip()
                if not re.fullmatch(r"znt_[A-Za-z0-9]{20,200}", value):
                    raise ReadError(401)
                return value
        except (OSError, UnicodeError):
            raise ReadError(401) from None

    def read(self, path):
        if not re.fullmatch(
            r"/pt/api/ai/jobs(?:\?page=\d+&page_size=100|/by-id/[a-z0-9]{25}"
            r"(?:/replicas\?page=1&page_size=100|/metrics\?minutes=30|/logs\?log_num=100)?)",
            path,
        ):
            raise ReadError(400)
        if self.config.origin is None:
            raise ReadError(503)
        with self.connections:
            token = self.token()
            now = time.monotonic()
            with self.lock:
                while self.requests and self.requests[0] <= now - 60:
                    self.requests.popleft()
                if now < self.cooldown_until:
                    raise ReadError(429, self.cooldown_until - now)
                if len(self.requests) >= 60:
                    raise ReadError(429, max(1, 60 - (now - self.requests[0])))
                self.requests.append(now)
            opener = build_opener(ProxyHandler({}), NoRedirect())
            req = Request(
                self.config.origin + path,
                headers={
                    "Authorization": "Bearer " + token,
                    "Accept": "application/json",
                },
            )
            try:
                with opener.open(req, timeout=10) as response:
                    payload = response.read(2_000_001)
                    if len(payload) > 2_000_000:
                        raise ReadError(502)
                    result = json.loads(payload, parse_constant=lambda _: None)
                    if not isinstance(result, dict):
                        raise ReadError(502)
                    return result
            except HTTPError as error:
                retry = 0
                if error.code == 429:
                    header = error.headers.get("Retry-After", "30")
                    try:
                        delay = float(header)
                    except ValueError:
                        try:
                            delay = (
                                parsedate_to_datetime(header).timestamp() - time.time()
                            )
                        except (ValueError, TypeError, OverflowError):
                            delay = 30
                    retry = min(86_400, max(30, delay))
                    with self.lock:
                        self.cooldown_until = time.monotonic() + retry
                raise ReadError(error.code, retry) from None
            except (URLError, TimeoutError, OSError, ValueError, RecursionError):
                raise ReadError(503) from None
