"""Staged SMTP diagnostics for StokIt (no secrets printed, no e-mail sent unless --send-to is given).

Usage (from django_backend):  python scripts/smtp_diagnose.py [--send-to ADDRESS]

Stages: 1 config, 2 DNS, 3 TCP connect, 4 TLS handshake (certificate verified), 5 SMTP login, 6 optional send.
Each stage reports only PASS/FAIL, the exception type and the SMTP reply code - never the password or message body.
"""
import argparse
import os
import smtplib
import socket
import ssl
import sys
from pathlib import Path
from email.message import EmailMessage

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = Path(os.environ.get("DJANGO_ENV_FILE") or BASE_DIR / ".env.postgres")
load_dotenv(dotenv_path=ENV_FILE, override=False)


def flag(name, default="0"):
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def report(stage, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {stage}" + (f" - {detail}" if detail else ""))
    return ok


def describe(exc):
    code = getattr(exc, "smtp_code", None)
    return type(exc).__name__ + (f" (SMTP {code})" if code else "")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--send-to", help="send one test message to this address (stage 6)")
    args = parser.parse_args()

    host = os.environ.get("EMAIL_HOST", "")
    port = int(os.environ.get("EMAIL_PORT", "587") or "587")
    user = os.environ.get("EMAIL_HOST_USER", "")
    password = os.environ.get("EMAIL_HOST_PASSWORD", "")
    sender = os.environ.get("DEFAULT_FROM_EMAIL", "")
    use_ssl, use_tls = flag("EMAIL_USE_SSL"), flag("EMAIL_USE_TLS", "1")
    timeout = int(os.environ.get("EMAIL_TIMEOUT", "20") or "20")

    problems = [n for n, v in (("EMAIL_HOST", host), ("EMAIL_HOST_USER", user), ("EMAIL_HOST_PASSWORD", password), ("DEFAULT_FROM_EMAIL", sender)) if not v]
    if use_ssl == use_tls:
        problems.append("exactly one of EMAIL_USE_SSL / EMAIL_USE_TLS must be enabled")
    if sender and user and sender.lower() != user.lower():
        problems.append("DEFAULT_FROM_EMAIL differs from EMAIL_HOST_USER (many providers reject this)")
    if not report("1 config", not problems, f"{host}:{port} mode={'SSL' if use_ssl else 'STARTTLS' if use_tls else 'none'} user={user} password_set={bool(password)}" + (f"; problems: {', '.join(problems)}" if problems else "")):
        return 1

    try:
        ip = socket.gethostbyname(host)
        report("2 DNS", True, ip)
    except OSError as exc:
        return 0 if report("2 DNS", False, describe(exc)) else 1

    try:
        socket.create_connection((host, port), timeout=timeout).close()
        report("3 TCP connect", True, f"port {port} reachable")
    except OSError as exc:
        report("3 TCP connect", False, describe(exc))
        return 1

    context = ssl.create_default_context()  # certificate + hostname verification stays ON
    try:
        if use_ssl:
            with socket.create_connection((host, port), timeout=timeout) as raw, context.wrap_socket(raw, server_hostname=host) as tls:
                report("4 TLS handshake", True, f"{tls.version()} cert verified")
            client = smtplib.SMTP_SSL(host, port, timeout=timeout, context=context)
        else:
            client = smtplib.SMTP(host, port, timeout=timeout)
            client.ehlo()
            client.starttls(context=context)
            client.ehlo()
            report("4 TLS handshake", True, "STARTTLS ok, cert verified")
    except Exception as exc:  # noqa: BLE001
        report("4 TLS handshake", False, describe(exc))
        return 1

    with client:
        try:
            client.login(user, password)
            report("5 SMTP login", True, "credentials accepted")
        except Exception as exc:  # noqa: BLE001
            report("5 SMTP login", False, describe(exc))
            return 1
        if not args.send_to:
            print("[SKIP] 6 send - pass --send-to ADDRESS to send one test message")
            return 0
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = "StokIt SMTP test", sender, args.send_to
        msg.set_content("This is a one-off StokIt SMTP connectivity test. No action is needed.")
        try:
            refused = client.send_message(msg)
            report("6 send", not refused, "accepted by SMTP server" if not refused else f"recipients refused: {len(refused)}")
            return 0 if not refused else 1
        except Exception as exc:  # noqa: BLE001
            report("6 send", False, describe(exc))
            return 1


if __name__ == "__main__":
    sys.exit(main())
