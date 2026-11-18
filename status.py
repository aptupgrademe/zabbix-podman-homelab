#!/usr/bin/env python3
"""Quick overview without the web UI: per host the latest values of the key checks, then open problems.

    python3 status.py

Shows ping, TCP ports, web scenarios, certificate days and agent ping. An item in error state
gets its error message appended, an item that never delivered a value is marked "(empty)".
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import zapi  # noqa: E402

token = zapi.login()


def C(method, params):
    return zapi.call(method, params, token)


KEYS = ("icmpping", "net.tcp", "web.test.fail", "cert.days", "agent.ping")
hosts = C("host.get", {"output": ["hostid", "name"]})
# item.get with webitems=True also returns the items of web scenarios (host.get does not)
all_items = C("item.get", {"output": ["hostid", "key_", "lastvalue", "state", "error", "lastclock"],
                           "hostids": [h["hostid"] for h in hosts], "webitems": True})
for host in hosts:
    items = [i for i in all_items if i["hostid"] == host["hostid"]
             and i["key_"].startswith(KEYS) and "loss" not in i["key_"] and "sec" not in i["key_"]]
    parts = []
    for i in items:
        s = f"{i['key_'][:38]}={i['lastvalue'][:12]}"
        if i["state"] == "1":
            s += " ERR:" + i["error"][:70]
        if i["lastclock"] == "0":
            s += " (empty)"
        parts.append(s)
    print(f"{host['name']}: " + " | ".join(parts))
print("problems:", [p["name"] for p in C("problem.get", {})] or "none")
