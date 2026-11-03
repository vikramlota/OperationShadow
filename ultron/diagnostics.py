"""Venue Wi-Fi & Gemini Endpoint Connectivity Diagnostics (`python -m ultron.diagnostics`).

Checks:
1. Presence of `GEMINI_API_KEY` (boolean only; NEVER prints or logs the key value).
2. DNS resolution timing for the Gemini API host (`generativelanguage.googleapis.com`).
3. TCP port 443 connect timing.
4. TLS handshake timing and negotiated TLS version.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import socket
import ssl
import sys
import time

DEFAULT_GEMINI_HOST = "generativelanguage.googleapis.com"
DEFAULT_GEMINI_PORT = 443


@dataclass
class GeminiDiagnosticsReport:
    host: str
    port: int
    timeout_sec: float
    api_key_present: bool
    dns_ok: bool
    dns_resolved_ip: str | None
    dns_elapsed_ms: float | None
    tcp_connect_ok: bool
    tcp_elapsed_ms: float | None
    tls_handshake_ok: bool
    tls_version: str | None
    tls_elapsed_ms: float | None
    error_category: str | None
    error_detail: str | None


def _load_dotenv_if_present() -> None:
    if "pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ:
        return
    try:
        import dotenv

        root_dir = Path(__file__).resolve().parent.parent
        for env_path in (root_dir / ".env", Path(".env")):
            if env_path.is_file():
                dotenv.load_dotenv(env_path, override=False)
                break
    except ImportError:
        pass


def run_gemini_diagnostics(
    host: str = DEFAULT_GEMINI_HOST,
    port: int = DEFAULT_GEMINI_PORT,
    timeout_sec: float = 3.0,
) -> GeminiDiagnosticsReport:
    """Run read-only DNS, TCP 443, and TLS handshake checks without printing or transmitting GEMINI_API_KEY."""
    _load_dotenv_if_present()
    api_key_present = bool((os.getenv("GEMINI_API_KEY") or "").strip())

    # 1. DNS Resolution
    t_dns = time.perf_counter()
    try:
        addr_info = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        dns_elapsed_ms = round((time.perf_counter() - t_dns) * 1000.0, 2)
        resolved_ip = str(addr_info[0][4][0]) if addr_info else None
        dns_ok = resolved_ip is not None
    except Exception as exc:
        dns_elapsed_ms = round((time.perf_counter() - t_dns) * 1000.0, 2)
        return GeminiDiagnosticsReport(
            host=host,
            port=port,
            timeout_sec=timeout_sec,
            api_key_present=api_key_present,
            dns_ok=False,
            dns_resolved_ip=None,
            dns_elapsed_ms=dns_elapsed_ms,
            tcp_connect_ok=False,
            tcp_elapsed_ms=None,
            tls_handshake_ok=False,
            tls_version=None,
            tls_elapsed_ms=None,
            error_category="dns",
            error_detail=f"{type(exc).__name__}: {exc}",
        )

    # 2. TCP Connect (port 443)
    t_tcp = time.perf_counter()
    raw_sock: socket.socket | None = None
    try:
        raw_sock = socket.create_connection((host, port), timeout=timeout_sec)
        tcp_elapsed_ms = round((time.perf_counter() - t_tcp) * 1000.0, 2)
    except Exception as exc:
        tcp_elapsed_ms = round((time.perf_counter() - t_tcp) * 1000.0, 2)
        if raw_sock is not None:
            raw_sock.close()
        return GeminiDiagnosticsReport(
            host=host,
            port=port,
            timeout_sec=timeout_sec,
            api_key_present=api_key_present,
            dns_ok=dns_ok,
            dns_resolved_ip=resolved_ip,
            dns_elapsed_ms=dns_elapsed_ms,
            tcp_connect_ok=False,
            tcp_elapsed_ms=tcp_elapsed_ms,
            tls_handshake_ok=False,
            tls_version=None,
            tls_elapsed_ms=None,
            error_category="tcp_connect",
            error_detail=f"{type(exc).__name__}: {exc}",
        )

    # 3. TLS Handshake
    t_tls = time.perf_counter()
    try:
        ctx = ssl.create_default_context()
        raw_sock.settimeout(timeout_sec)
        with ctx.wrap_socket(raw_sock, server_hostname=host) as tls_sock:
            tls_version = tls_sock.version()
            tls_elapsed_ms = round((time.perf_counter() - t_tls) * 1000.0, 2)
            return GeminiDiagnosticsReport(
                host=host,
                port=port,
                timeout_sec=timeout_sec,
                api_key_present=api_key_present,
                dns_ok=dns_ok,
                dns_resolved_ip=resolved_ip,
                dns_elapsed_ms=dns_elapsed_ms,
                tcp_connect_ok=True,
                tcp_elapsed_ms=tcp_elapsed_ms,
                tls_handshake_ok=True,
                tls_version=tls_version,
                tls_elapsed_ms=tls_elapsed_ms,
                error_category=None,
                error_detail=None,
            )
    except Exception as exc:
        tls_elapsed_ms = round((time.perf_counter() - t_tls) * 1000.0, 2)
        try:
            raw_sock.close()
        except Exception:
            pass
        return GeminiDiagnosticsReport(
            host=host,
            port=port,
            timeout_sec=timeout_sec,
            api_key_present=api_key_present,
            dns_ok=dns_ok,
            dns_resolved_ip=resolved_ip,
            dns_elapsed_ms=dns_elapsed_ms,
            tcp_connect_ok=True,
            tcp_elapsed_ms=tcp_elapsed_ms,
            tls_handshake_ok=False,
            tls_version=None,
            tls_elapsed_ms=tls_elapsed_ms,
            error_category="tls_handshake",
            error_detail=f"{type(exc).__name__}: {exc}",
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ultron.diagnostics",
        description="Check Gemini API key presence, DNS, TCP 443, and TLS handshake timing.",
    )
    parser.add_argument("--host", default=DEFAULT_GEMINI_HOST, help="Target hostname")
    parser.add_argument("--port", type=int, default=DEFAULT_GEMINI_PORT, help="Target TCP port")
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(os.getenv("SHADOW_LLM_CONNECT_TIMEOUT_SEC", "3.0")),
        help="Per-stage socket timeout in seconds",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    args = parser.parse_args(argv)

    report = run_gemini_diagnostics(
        host=args.host,
        port=args.port,
        timeout_sec=args.timeout,
    )
    if args.json:
        print(json.dumps(asdict(report), indent=2))
    else:
        print("=== ULTRON Gemini Connectivity Diagnostics ===")
        print(f"Target Host      : {report.host}:{report.port} (timeout={report.timeout_sec:.1f}s)")
        print(f"GEMINI_API_KEY   : {'PRESENT (redacted)' if report.api_key_present else 'UNSET'}")
        print(
            f"DNS Resolution   : {'OK' if report.dns_ok else 'FAIL'} "
            f"({report.dns_elapsed_ms} ms, ip={report.dns_resolved_ip})"
        )
        print(
            f"TCP 443 Connect  : {'OK' if report.tcp_connect_ok else 'FAIL'} "
            f"({report.tcp_elapsed_ms} ms)"
        )
        print(
            f"TLS Handshake    : {'OK' if report.tls_handshake_ok else 'FAIL'} "
            f"({report.tls_elapsed_ms} ms, version={report.tls_version})"
        )
        if report.error_category:
            print(f"Failure Category : {report.error_category} ({report.error_detail})")

    return 0 if (report.dns_ok and report.tcp_connect_ok and report.tls_handshake_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
