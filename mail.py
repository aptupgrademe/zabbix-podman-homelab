#!/usr/bin/env python3
"""Make a fresh Zabbix send mail: SMTP settings, the Admin's address, and the default action.

    MAIL_TO=you@example.org python3 mail.py

Out of the box Zabbix sends no mail at all: the media type "Email" is not configured, the
Admin user has no address, and the action "Report problems to Zabbix administrators" is
disabled. This script fixes all three.

The SMTP settings are read from an msmtp configuration (host, port, from, user, password,
tls_starttls), by default ~/.msmtprc - the same one the server already uses for cron mail.
Optional: MSMTPRC=<file>, SMTP_HELO=<your domain>.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import zapi  # noqa: E402

to = os.environ.get("MAIL_TO") or sys.exit("set MAIL_TO=<address>")
rc = os.path.expanduser(os.environ.get("MSMTPRC", "~/.msmtprc"))
cfg = dict(re.findall(r"^\s*(\w+)\s+(.+?)\s*$", open(rc).read(), re.M))
helo = os.environ.get("SMTP_HELO", cfg["from"].split("@")[-1])

token = zapi.login()


def C(method, params):
    return zapi.call(method, params, token)


mt = C("mediatype.get", {"filter": {"name": ["Email"]}})[0]
C("mediatype.update", {
    "mediatypeid": mt["mediatypeid"], "status": 0,
    "smtp_server": cfg["host"], "smtp_port": cfg["port"], "smtp_helo": helo, "smtp_email": cfg["from"],
    # 1 = STARTTLS, 2 = SSL/TLS (port 465, "tls_starttls off" in msmtp)
    "smtp_security": 2 if cfg.get("tls_starttls") == "off" else 1,
    "smtp_verify_peer": 1, "smtp_verify_host": 1,
    "smtp_authentication": 1, "username": cfg["user"], "passwd": cfg["password"],
    "content_type": 0})

admin = C("user.get", {"filter": {"username": zapi.USER}})[0]
# severity 63 = all six levels; period = always
C("user.update", {"userid": admin["userid"], "medias": [{
    "mediatypeid": mt["mediatypeid"], "sendto": [to], "active": 0, "severity": 63, "period": "1-7,00:00-24:00"}]})

action = C("action.get", {"filter": {"name": ["Report problems to Zabbix administrators"]}})[0]
C("action.update", {"actionid": action["actionid"], "status": 0})
print(f"mail configured for {to}, default action enabled")
# Zabbix 7.0 has no mediatype.test in the API: test with a real (staged) outage.
