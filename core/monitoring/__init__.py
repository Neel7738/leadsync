"""
Lightweight Prometheus-compatible metrics exporter.

No external dependencies — uses only stdlib to format the Prometheus
text exposition format. Tracks request counts, latencies, queue depth,
LLM provider health, and custom business metrics.

Endpoint: GET /metrics  →  Prometheus text format
"""

import time
import threading
from typing import Dict, Any, Optional
from datetime import datetime
from collections import defaultdict


class _Metrics:
    """Thread-safe in-memory metrics store."""

    def __init__(self):
        self._lock = threading.Lock()
        self._counters: Dict[str, float] = defaultdict(float)
        self._gauges: Dict[str, float] = defaultdict(float)
        self._histograms: Dict[str, list] = defaultdict(list)
        self._labels: Dict[str, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
        self._start_time = time.time()

    def inc(self, name: str, value: float = 1.0, labels: Optional[Dict[str, str]] = None) -> None:
        """Increment a counter."""
        with self._lock:
            key = self._label_key(name, labels)
            self._counters[key] += value

    def gauge(self, name: str, value: float, labels: Optional[Dict[str, str]] = None) -> None:
        """Set a gauge value."""
        with self._lock:
            key = self._label_key(name, labels)
            self._gauges[key] = value

    def observe(self, name: str, value: float, labels: Optional[Dict[str, str]] = None) -> None:
        """Record a histogram observation."""
        with self._lock:
            key = self._label_key(name, labels)
            self._histograms[key].append(value)
            # Keep last 1000 observations
            if len(self._histograms[key]) > 1000:
                self._histograms[key] = self._histograms[key][-1000:]

    def _label_key(self, name: str, labels: Optional[Dict[str, str]] = None) -> str:
        if not labels:
            return name
        sorted_labels = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
        return f"{name}{{{sorted_labels}}}"

    def export(self) -> str:
        """Export all metrics in Prometheus text exposition format."""
        lines = []
        lines.append("# HELP sfa_uptime_seconds Time since process start")
        lines.append("# TYPE sfa_uptime_seconds gauge")
        lines.append(f"sfa_uptime_seconds {time.time() - self._start_time:.2f}")
        lines.append("")

        with self._lock:
            # Counters
            for key, value in sorted(self._counters.items()):
                metric_name = key.split("{")[0] if "{" in key else key
                lines.append(f"# TYPE {metric_name} counter")
                lines.append(f"{key} {value:.2f}")
            lines.append("")

            # Gauges
            for key, value in sorted(self._gauges.items()):
                metric_name = key.split("{")[0] if "{" in key else key
                lines.append(f"# TYPE {metric_name} gauge")
                lines.append(f"{key} {value:.2f}")
            lines.append("")

            # Histograms
            for key, observations in sorted(self._histograms.items()):
                metric_name = key.split("{")[0] if "{" in key else key
                lines.append(f"# TYPE {metric_name} histogram")
                if observations:
                    sorted_obs = sorted(observations)
                    count = len(sorted_obs)
                    total = sum(sorted_obs)
                    lines.append(f"{key}_count {count}")
                    lines.append(f"{key}_sum {total:.4f}")
                    # Bucket boundaries
                    for bucket in [0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0]:
                        cumcount = sum(1 for o in sorted_obs if o <= bucket)
                        label_part = key[key.index("{"):] if "{" in key else ""
                        lines.append(f'{metric_name}_bucket{{le="{bucket}"{label_part[1:]}}}{cumcount}')
                    lines.append(f'{metric_name}_bucket{{le="+Inf"{key[key.index("{"):][1:] if "{" in key else ""}}}{count}')
            lines.append("")

        return "\n".join(lines)


# Global singleton
metrics = _Metrics()


def record_request(method: str, path: str, status: int, duration: float) -> None:
    """Record an HTTP request metric."""
    labels = {"method": method, "path": path, "status": str(status)}
    metrics.inc("sfa_http_requests_total", labels=labels)
    metrics.observe("sfa_http_request_duration_seconds", duration, labels=labels)


def record_llm_call(provider: str, model: str, latency: float, success: bool) -> None:
    """Record an LLM call metric."""
    labels = {"provider": provider, "model": model, "success": str(success).lower()}
    metrics.inc("sfa_llm_calls_total", labels=labels)
    metrics.observe("sfa_llm_call_duration_seconds", latency, labels=labels)


def record_queue_depth(depth: int) -> None:
    """Record current queue depth."""
    metrics.gauge("sfa_queue_depth", depth)


def record_sla_breach(count: int) -> None:
    """Record SLA breach count."""
    metrics.gauge("sfa_sla_breaches", count)


def record_draft_generated(tone: str) -> None:
    """Record a draft generation event."""
    metrics.inc("sfa_drafts_generated_total", labels={"tone": tone})


def record_email_sent(status: str) -> None:
    """Record an email send event."""
    metrics.inc("sfa_emails_sent_total", labels={"status": status})
