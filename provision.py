#!/usr/bin/env python3
"""Create Zabbix hosts, checks and triggers from a YAML file through the API.

    python3 provision.py hosts.yml

Idempotent in a simple way: a host that already exists is skipped completely
(its checks are not compared or changed). Delete a host in the UI to have it
recreated from the file.

What it creates:
- LAN devices: ICMP ping (template "ICMP Ping") and optional TCP port checks.
  A port trigger depends on the ping trigger, so a dead device sends one mail,
  not one per port.
- External sites: web scenarios (HTTP 200 + expected text) and the remaining
  days of their TLS certificates, read by the local agent 2 (web.certificate.get).
- Optionally renames the default host "Zabbix server" (agent on 127.0.0.1).
"""
import os
import re
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import zapi  # noqa: E402

if len(sys.argv) != 2:
    raise SystemExit(__doc__)
cfg = yaml.safe_load(open(sys.argv[1]))

token = zapi.login()


def C(method, params):
    return zapi.call(method, params, token)


def group(name):
    g = C("hostgroup.get", {"filter": {"name": [name]}})
    return g[0]["groupid"] if g else C("hostgroup.create", {"name": name})["groupids"][0]


def template(name):
    return C("template.get", {"filter": {"host": [name]}})[0]["templateid"]


def tech(name):
    # Zabbix allows only [A-Za-z0-9 ._-] in the technical host name; the visible name keeps the original
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")


def mkhost(name, address, groupid, templates, agent=False):
    h = C("host.get", {"filter": {"host": [tech(name)]}})
    if h:
        print("exists:", name)
        return h[0]["hostid"], False
    # external sites are checked from this machine: their interface is the local agent
    itf = [{"type": 1, "main": 1, "useip": 1, "ip": "127.0.0.1" if agent else address, "dns": "", "port": "10050"}]
    hid = C("host.create", {"host": tech(name), "name": name, "groups": [{"groupid": groupid}], "interfaces": itf,
                            "templates": [{"templateid": t} for t in templates],
                            "tags": [{"tag": "address", "value": address}]})["hostids"][0]
    print("created:", name)
    return hid, True


def iface(hid):
    return C("hostinterface.get", {"hostids": hid})[0]["interfaceid"]


# --- LAN devices -------------------------------------------------------------
lan_group = group(cfg.get("lan_group", "LAN"))
icmp = template("ICMP Ping")
for dev in cfg.get("lan", []):
    name, ip, ping, ports = dev["name"], dev["ip"], dev.get("ping", True), dev.get("tcp_ports", [])
    hid, new = mkhost(name, ip, lan_group, [icmp] if ping else [])
    if not new:
        continue
    pingtrig = C("trigger.get", {"hostids": hid, "search": {"description": "Unavailable by ICMP ping"}}) if ping else []
    for port in ports:
        key = f"net.tcp.service[tcp,{ip},{port}]"
        C("item.create", {"hostid": hid, "name": f"TCP port {port} reachable", "key_": key, "type": 3,
                          "value_type": 3, "delay": "1m", "interfaceid": iface(hid)})
        trig = {"description": f"{name}: port {port} not answering", "priority": 4,
                "expression": f"max(/{tech(name)}/{key},#3)=0",
                "comments": f"3 failures in a row (1 min interval) on {ip}:{port}"}
        if pingtrig:
            trig["dependencies"] = [{"triggerid": pingtrig[0]["triggerid"]}]
        C("trigger.create", trig)

# --- the monitoring server itself -------------------------------------------
rename = cfg.get("rename_zabbix_server")
if rename:
    zs = C("host.get", {"filter": {"host": ["Zabbix server"]}})
    if zs:
        C("host.update", {"hostid": zs[0]["hostid"], "host": tech(rename["host"]), "name": rename["name"]})
        print("renamed: Zabbix server ->", rename["name"])

# --- external sites: web scenarios + certificate days -------------------------
ext_group = group(cfg.get("external_group", "External"))
for site in cfg.get("external", []):
    name, fqdn, steps = site["name"], site["fqdn"], site["checks"]
    hid, new = mkhost(name, fqdn, ext_group, [], agent=True)
    if not new:
        continue
    for step in steps:
        sname, url, required = step["name"], step["url"], step["expect"]
        C("httptest.create", {"hostid": hid, "name": sname, "delay": "2m", "retries": 2, "agent": "Zabbix homelab",
                              "steps": [{"name": sname, "url": url, "status_codes": "200", "required": required,
                                         "no": 1, "timeout": "20s"}]})
        C("trigger.create", {"description": f"{name}: {sname} unreachable or wrong", "priority": 4,
                             "expression": f"min(/{tech(name)}/web.test.fail[{sname}],#2)>0",
                             "comments": f"{url} - 2 runs in a row failed; see web.test.error[{sname}]"})
    hosts = sorted({fqdn} | {re.sub(r"https://([^/]+)/.*", r"\1", s["url"]) for s in steps})
    for h in hosts:
        key = f"web.certificate.get[{h},443]"
        master = C("item.create", {"hostid": hid, "name": f"Certificate {h} (raw)", "key_": key, "type": 0,
                                   "value_type": 4, "delay": "6h", "interfaceid": iface(hid), "history": "7d"})["itemids"][0]
        C("item.create", {"hostid": hid, "name": f"Certificate {h}: days valid", "key_": f"cert.days[{h}]", "type": 18,
                          "master_itemid": master, "value_type": 0, "units": "d",
                          "preprocessing": [
                              {"type": 12, "params": "$.x509.not_after.timestamp", "error_handler": 0, "error_handler_params": ""},
                              {"type": 21, "params": "return ((value - Date.now()/1000)/86400).toFixed(1);",
                               "error_handler": 0, "error_handler_params": ""}]})
        C("trigger.create", {"description": f"{name}: certificate {h} expires in less than 14 days", "priority": 3,
                             "expression": f"last(/{tech(name)}/cert.days[{h}])<14"})
print("done")
