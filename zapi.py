"""Tiny Zabbix JSON-RPC client (Zabbix 7.0).

Settings come from the environment:
  ZABBIX_URL        API endpoint     (default http://127.0.0.1:8080/api_jsonrpc.php)
  ZABBIX_USER       user name        (default Admin)
  ZABBIX_PASS_FILE  file holding the password, mode 0600 (default ~/.config/zabbix/admin.pass)

On a fresh installation the Admin password is still the default "zabbix".
login() then changes it to the one in ZABBIX_PASS_FILE and logs in with it.
"""
import json
import os
import urllib.request

URL = os.environ.get("ZABBIX_URL", "http://127.0.0.1:8080/api_jsonrpc.php")
USER = os.environ.get("ZABBIX_USER", "Admin")
PASS_FILE = os.path.expanduser(os.environ.get("ZABBIX_PASS_FILE", "~/.config/zabbix/admin.pass"))


def call(method, params, token=None):
    headers = {"Content-Type": "application/json-rpc"}
    if token:
        headers["Authorization"] = "Bearer " + token
    body = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": 1}).encode()
    reply = json.load(urllib.request.urlopen(urllib.request.Request(URL, body, headers), timeout=30))
    if "error" in reply:
        raise SystemExit(f"{method}: {reply['error']}")
    return reply["result"]


def login():
    password = open(PASS_FILE).read().strip()
    try:
        return call("user.login", {"username": USER, "password": password})
    except SystemExit:
        # first run: still the default password -> replace it
        token = call("user.login", {"username": USER, "password": "zabbix"})
        uid = call("user.get", {"filter": {"username": USER}}, token)[0]["userid"]
        call("user.update", {"userid": uid, "current_passwd": "zabbix", "passwd": password}, token)
        print(f"password of {USER} changed")
        return call("user.login", {"username": USER, "password": password})
