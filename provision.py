#!/usr/bin/env python3
"""Create and maintain Zabbix hosts, checks and triggers from a YAML file through the API.

    python3 provision.py hosts.yml                      create what is missing, leave existing hosts alone
    python3 provision.py hosts.yml --update             also bring existing hosts in line with the file
    python3 provision.py hosts.yml --update "Router"    ... only these hosts (visible names from the file)
    python3 provision.py hosts.yml --delete "Old plug"  delete these hosts (with their history)
    add --dry-run to any of them to see what would change without changing anything

The file is the source of truth for what it describes. A typical change without the web UI:
edit hosts.yml, run with --update "Name" --dry-run, read the plan, run again without --dry-run.

What it creates:
- LAN devices: ICMP ping (template "ICMP Ping") and optional TCP port checks.
  A port trigger depends on the ping trigger, so a dead device sends one mail,
  not one per port.
- External sites: web scenarios (HTTP 200 + expected text) and the remaining
  days of their TLS certificates, read by the local agent 2 (web.certificate.get).
- Optionally renames the default host "Zabbix server" (agent on 127.0.0.1).

What --update changes on an existing host: the IP address, ping on/off, added or removed TCP ports,
changed web checks and certificate host names. History is kept for everything that stays the same;
a check whose key changes (a port check after a new IP, a changed web check) starts with fresh history.
The visible name is the key: renaming a device in the file means --delete the old name, then create the new one.
"""
import os
import re
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import zapi  # noqa: E402

args = [a for a in sys.argv[1:] if a != "--dry-run"]
DRY = "--dry-run" in sys.argv
if not args or args[0].startswith("--"):
    raise SystemExit(__doc__)
cfg = yaml.safe_load(open(args[0]))
MODE, NAMES = "create", []
if len(args) > 1:
    if args[1] not in ("--update", "--delete"):
        raise SystemExit(__doc__)
    MODE, NAMES = args[1][2:], args[2:]
    if MODE == "delete" and not NAMES:
        raise SystemExit("--delete needs at least one host name")

token = zapi.login()
WRITE = re.compile(r"\.(create|update|delete|massadd|massremove)$")


def C(method, params):
    if DRY and WRITE.search(method):
        print(f"  would call {method}: {str(params)[:160]}")
        return {k: ["0"] for k in ("hostids", "itemids", "triggerids", "httptestids", "groupids", "interfaceids")}
    return zapi.call(method, params, token)


def group(name):
    g = C("hostgroup.get", {"filter": {"name": [name]}})
    return g[0]["groupid"] if g else C("hostgroup.create", {"name": name})["groupids"][0]


def template(name):
    return C("template.get", {"filter": {"host": [name]}})[0]["templateid"]


def tech(name):
    # Zabbix allows only [A-Za-z0-9 ._-] in the technical host name; the visible name keeps the original
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")


def find(name):
    h = C("host.get", {"filter": {"host": [tech(name)]}, "selectInterfaces": ["interfaceid", "ip"],
                       "selectParentTemplates": ["templateid"], "selectTags": "extend"})
    return h[0] if h else None


def mkhost(name, address, groupid, templates, agent=False):
    # external sites are checked from this machine: their interface is the local agent
    itf = [{"type": 1, "main": 1, "useip": 1, "ip": "127.0.0.1" if agent else address, "dns": "", "port": "10050"}]
    hid = C("host.create", {"host": tech(name), "name": name, "groups": [{"groupid": groupid}], "interfaces": itf,
                            "templates": [{"templateid": t} for t in templates],
                            "tags": [{"tag": "address", "value": address}]})["hostids"][0]
    print("created:", name)
    return hid


def iface(hid):
    i = C("hostinterface.get", {"hostids": hid})
    return i[0]["interfaceid"] if i else "0"


def wanted(name):
    return MODE == "update" and (not NAMES or name in NAMES)


# --- LAN devices -------------------------------------------------------------
def port_checks(hid, name, ip, ports):
    pingtrig = C("trigger.get", {"hostids": hid, "search": {"description": "Unavailable by ICMP ping"}})
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


def update_lan(h, name, ip, ping, ports, icmp):
    hid, changed = h["hostid"], False
    itf = h["interfaces"][0]
    if itf["ip"] != ip:
        print(f"  {name}: address {itf['ip']} -> {ip}")
        C("hostinterface.update", {"interfaceid": itf["interfaceid"], "ip": ip})
        C("host.update", {"hostid": hid, "tags": [t for t in h["tags"] if t["tag"] != "address"]
                          + [{"tag": "address", "value": ip}]})
        changed = True
    linked = any(t["templateid"] == icmp for t in h["parentTemplates"])
    if ping and not linked:
        print(f"  {name}: ping on")
        C("host.massadd", {"hosts": [{"hostid": hid}], "templates": [{"templateid": icmp}]})
        changed = True
    elif not ping and linked:
        print(f"  {name}: ping off")
        C("host.update", {"hostid": hid, "templates_clear": [{"templateid": icmp}]})
        changed = True
    have = {i["key_"]: i["itemid"] for i in C("item.get", {"hostids": hid, "output": ["key_"],
                                                           "search": {"key_": "net.tcp.service[tcp,"}})}
    want = {f"net.tcp.service[tcp,{ip},{p}]": p for p in ports}
    drop = [iid for k, iid in have.items() if k not in want]   # removed ports, or all of them after a new IP
    add = [p for k, p in want.items() if k not in have]
    if drop:
        print(f"  {name}: remove {len(drop)} port check(s)")
        C("item.delete", drop)                                  # their triggers go with them
        changed = True
    if add:
        print(f"  {name}: add port check(s) {add}")
        port_checks(hid, name, ip, add)
        changed = True
    print(("updated: " if changed else "unchanged: ") + name)


if MODE == "delete":
    for name in NAMES:
        h = find(name)
        if not h:
            print("not found:", name)
            continue
        C("host.delete", [h["hostid"]])
        print("deleted:", name)
    raise SystemExit(0)

lan_group = group(cfg.get("lan_group", "LAN"))
icmp = template("ICMP Ping")
for dev in cfg.get("lan", []):
    name, ip, ping, ports = dev["name"], dev["ip"], dev.get("ping", True), dev.get("tcp_ports", [])
    h = find(name)
    if h:
        if wanted(name):
            update_lan(h, name, ip, ping, ports, icmp)
        elif MODE == "create":
            print("exists:", name)
        continue
    hid = mkhost(name, ip, lan_group, [icmp] if ping else [])
    port_checks(hid, name, ip, ports)

# --- the monitoring server itself -------------------------------------------
rename = cfg.get("rename_zabbix_server")
if rename:
    zs = C("host.get", {"filter": {"host": ["Zabbix server"]}})
    if zs:
        C("host.update", {"hostid": zs[0]["hostid"], "host": tech(rename["host"]), "name": rename["name"]})
        print("renamed: Zabbix server ->", rename["name"])


# --- external sites: web scenarios + certificate days -------------------------
def cert_hosts(fqdn, steps):
    return sorted({fqdn} | {re.sub(r"https://([^/]+)/.*", r"\1", s["url"]) for s in steps})


def web_checks(hid, name, steps):
    for step in steps:
        sname, url, required = step["name"], step["url"], step["expect"]
        C("httptest.create", {"hostid": hid, "name": sname, "delay": "2m", "retries": 2, "agent": "Zabbix homelab",
                              "steps": [{"name": sname, "url": url, "status_codes": "200", "required": required,
                                         "no": 1, "timeout": "20s"}]})
        C("trigger.create", {"description": f"{name}: {sname} unreachable or wrong", "priority": 4,
                             "expression": f"min(/{tech(name)}/web.test.fail[{sname}],#2)>0",
                             "comments": f"{url} - 2 runs in a row failed; see web.test.error[{sname}]"})


def cert_checks(hid, name, hosts):
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


def update_external(h, name, steps, hosts):
    hid, changed = h["hostid"], False
    tests = C("httptest.get", {"hostids": hid, "output": ["name"], "selectSteps": ["url", "required"]})
    have = sorted((t["name"], t["steps"][0]["url"], t["steps"][0]["required"]) for t in tests)
    want = sorted((s["name"], s["url"], s["expect"]) for s in steps)
    if have != want:                                            # simplest correct way: rebuild the web checks
        print(f"  {name}: web checks changed, rebuilding {len(want)}")
        if tests:
            C("httptest.delete", [t["httptestid"] for t in tests])
        web_checks(hid, name, steps)
        changed = True
    raw = {i["key_"]: i["itemid"] for i in C("item.get", {"hostids": hid, "output": ["key_"],
                                                          "search": {"key_": "web.certificate.get["}})}
    want_c = {f"web.certificate.get[{x},443]": x for x in hosts}
    drop = [iid for k, iid in raw.items() if k not in want_c]
    add = [x for k, x in want_c.items() if k not in raw]
    if drop:
        print(f"  {name}: remove {len(drop)} certificate check(s)")
        C("item.delete", drop)                                  # dependent days item and trigger go with them
        changed = True
    if add:
        print(f"  {name}: add certificate check(s) {add}")
        cert_checks(hid, name, add)
        changed = True
    print(("updated: " if changed else "unchanged: ") + name)


ext_group = group(cfg.get("external_group", "External"))
for site in cfg.get("external", []):
    name, fqdn, steps = site["name"], site["fqdn"], site["checks"]
    h = find(name)
    if h:
        if wanted(name):
            update_external(h, name, steps, cert_hosts(fqdn, steps))
        elif MODE == "create":
            print("exists:", name)
        continue
    hid = mkhost(name, fqdn, ext_group, [], agent=True)
    web_checks(hid, name, steps)
    cert_checks(hid, name, cert_hosts(fqdn, steps))
print("done" + (" (dry run, nothing changed)" if DRY else ""))
