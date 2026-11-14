# src/astock/data/rate_limiter.py
import math
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, TypeVar

T = TypeVar("T")


class RateLimitExceeded(Exception):
    pass


class CircuitBreakerOpen(Exception):
    pass


@dataclass
class RateLimitConfig:
    enabled: bool = True
    requests_per_second: float = 5.0
    max_concurrency: int = 2
    retry_times: int = 3
    backoff_factor: float = 1.5
    jitter_enabled: bool = True
    failure_threshold: int = 5
    circuit_cooldown_seconds: float = 60.0


@dataclass
class ProviderMetrics:
    provider: str
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    rate_limit_hits: int = 0
    circuit_trips: int = 0
    total_duration_ms: float = 0.0
    last_error: Optional[str] = None
    last_error_time: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        avg_ms = (self.total_duration_ms / self.total_requests) if self.total_requests > 0 else 0.0
        rate = (self.successful_requests / self.total_requests * 100) if self.total_requests > 0 else 100.0
        return {
            "provider": self.provider,
            "total_requests": self.total_requests,
            "successful_requests": self.successful_requests,
            "failed_requests": self.failed_requests,
            "success_rate_pct": round(rate, 2),
            "rate_limit_hits": self.rate_limit_hits,
            "circuit_trips": self.circuit_trips,
            "avg_duration_ms": round(avg_ms, 2),
            "last_error": self.last_error
        }


class ProviderCircuitBreaker:
    """Three-state circuit breaker: CLOSED, OPEN, HALF_OPEN."""
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"

    def __init__(self, failure_threshold: int = 5, cooldown_seconds: float = 60.0,
                 provider_name: str = ""):
        self.provider_name = provider_name
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.state = self.CLOSED
        self.failure_count = 0
        self.open_until = 0.0
        self._lock = threading.Lock()

    def can_attempt(self) -> bool:
        with self._lock:
            now = time.monotonic()
            if self.state == self.CLOSED:
                return True
            if self.state == self.OPEN:
                if now >= self.open_until:
                    self.state = self.HALF_OPEN
                    return True
                return False
            # HALF_OPEN allows single probe
            return True

    allow_request = can_attempt

    def record_success(self):
        with self._lock:
            self.failure_count = 0
            self.state = self.CLOSED

    def record_failure(self):
        with self._lock:
            self.failure_count += 1
            if self.failure_count >= self.failure_threshold or self.state == self.HALF_OPEN:
                self.state = self.OPEN
                self.open_until = time.monotonic() + self.cooldown_seconds


class SmartRateLimiter:
    """Token bucket / sliding window rate limiter with concurrency guard and circuit breaker."""

    def __init__(self, provider: str, config: Optional[RateLimitConfig] = None):
        self.provider = provider
        self.config = config or RateLimitConfig()
        self.circuit = ProviderCircuitBreaker(
            failure_threshold=self.config.failure_threshold,
            cooldown_seconds=self.config.circuit_cooldown_seconds
        )
        self.metrics = ProviderMetrics(provider=provider)
        self._lock = threading.Lock()
        self._semaphore = threading.Semaphore(self.config.max_concurrency)
        self._last_request_time = 0.0
        self._min_interval = 1.0 / self.config.requests_per_second if self.config.requests_per_second > 0 else 0.0

    def execute(self, func: Callable[[], T]) -> T:
        """Executes func with rate limiting, concurrency cap, retries, and circuit breaking."""
        if not self.circuit.can_attempt():
            with self._lock:
                self.metrics.circuit_trips += 1
            raise CircuitBreakerOpen(f"Circuit for provider '{self.provider}' is OPEN")

        attempts = 0
        max_attempts = self.config.retry_times + 1
        last_exc = None

        while attempts < max_attempts:
            attempts += 1
            # 1. Enforce rate limit interval
            if self.config.enabled and self._min_interval > 0:
                with self._lock:
                    now = time.monotonic()
                    elapsed = now - self._last_request_time
                    wait_time = self._min_interval - elapsed
                    if wait_time > 0:
                        self.metrics.rate_limit_hits += 1
                    else:
                        wait_time = 0.0
                    self._last_request_time = now + wait_time

                if wait_time > 0:
                    time.sleep(wait_time)

            # 2. Concurrency acquire
            with self._semaphore:
                t0 = time.monotonic()
                try:
                    with self._lock:
                        self.metrics.total_requests += 1

                    result = func()

                    duration_ms = (time.monotonic() - t0) * 1000
                    with self._lock:
                        self.metrics.successful_requests += 1
                        self.metrics.total_duration_ms += duration_ms

                    self.circuit.record_success()
                    return result

                except Exception as exc:
                    last_exc = exc
                    duration_ms = (time.monotonic() - t0) * 1000
                    with self._lock:
                        self.metrics.failed_requests += 1
                        self.metrics.total_duration_ms += duration_ms
                        self.metrics.last_error = f"{type(exc).__name__}: {str(exc)}"
                        self.metrics.last_error_time = time.time()

                    if attempts >= max_attempts:
                        self.circuit.record_failure()
                        break

                    # Exponential backoff with jitter
                    backoff = self.config.backoff_factor ** attempts
                    if self.config.jitter_enabled:
                        backoff *= random.uniform(0.7, 1.3)
                    time.sleep(backoff)

        raise last_exc
