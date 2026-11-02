#!/usr/bin/env python3
"""Import (create) or update the two workflows in a running n8n instance.

Reads config.env for N8N_URL / N8N_EMAIL / N8N_PASSWORD / WF_A_ID / WF_B_ID.
- If WF_A_ID / WF_B_ID are BLANK  -> the workflow is CREATED and its new id is printed
                                     (paste the id back into config.env to update it later).
- If they are SET                 -> the workflow is stopped (WF-B), deactivated, PATCHed, reactivated.

Run  `python3 build_wf.py wf_a.json wf_b.json`  first to (re)generate the JSON.

Usage:  python3 import_workflows.py
"""
import json, os, sys, time, urllib.request, urllib.error, http.cookiejar
from urllib.parse import quote

def load_config():
    here = os.path.dirname(os.path.abspath(__file__))
    p = os.path.join(here, "config.env")
    if os.path.exists(p):
        for line in open(p):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
load_config()

N8N = os.environ.get("N8N_URL", "http://127.0.0.1:5678").rstrip("/")
EMAIL = os.environ.get("N8N_EMAIL", "")
PW = os.environ.get("N8N_PASSWORD", "")
if not EMAIL or not PW:
    sys.exit("ERROR: N8N_EMAIL / N8N_PASSWORD missing in config.env")

opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(N8N + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with opener.open(r, timeout=40) as resp:
            raw = resp.read().decode()
            return resp.getcode(), (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode()[:300]}

def login():
    code, _ = req("POST", "/rest/login", {"emailOrLdapLoginId": EMAIL, "password": PW})
    if code != 200:
        sys.exit("Login failed (%s)" % code)
    print("logged in to n8n at", N8N)

def running_execs(wf_id):
    code, r = req("GET", "/rest/executions?filter=" + quote(json.dumps({"workflowId": wf_id})))
    rows = r.get("data", r)
    if isinstance(rows, dict):
        rows = rows.get("results", [])
    return [x["id"] for x in (rows or []) if x.get("status") == "running"]

def deploy(tag, wf_file, id_key):
    wf = json.load(open(wf_file))
    payload = {"name": wf["name"], "nodes": wf["nodes"],
               "connections": wf["connections"], "settings": wf.get("settings", {})}
    wf_id = os.environ.get(id_key, "").strip()

    if not wf_id:   # CREATE
        code, r = req("POST", "/rest/workflows", payload)
        if code not in (200, 201):
            print("  %s CREATE FAILED %s %s" % (tag, code, r)); return
        new_id = r["data"]["id"]; vid = r["data"]["versionId"]
        req("POST", "/rest/workflows/%s/activate" % new_id, {"versionId": vid})
        print("  %s CREATED id=%s  -> put  %s=%s  in config.env" % (tag, new_id, id_key, new_id))
        return

    # UPDATE existing
    for ex in running_execs(wf_id):     # stop any running listener first (WF-B)
        req("POST", "/rest/executions/%s/stop" % ex, {})
        print("  %s stopped running execution %s" % (tag, ex))
    code, cur = req("GET", "/rest/workflows/%s" % wf_id)
    if code != 200:
        print("  %s GET FAILED %s (is WF id correct?)" % (tag, code)); return
    vid = cur["data"]["versionId"]
    req("POST", "/rest/workflows/%s/deactivate" % wf_id, {"versionId": vid})
    payload["versionId"] = vid
    code, r = req("PATCH", "/rest/workflows/%s" % wf_id, payload)
    if code != 200:
        print("  %s PATCH FAILED %s %s" % (tag, code, r)); return
    nv = r["data"]["versionId"]
    code, r = req("POST", "/rest/workflows/%s/activate" % wf_id, {"versionId": nv})
    print("  %s UPDATED id=%s active=%s nodes=%d" % (tag, wf_id, r.get("data", {}).get("active"), len(wf["nodes"])))

if __name__ == "__main__":
    for f in ("wf_a.json", "wf_b.json"):
        if not os.path.exists(f):
            sys.exit("ERROR: %s not found — run `python3 build_wf.py wf_a.json wf_b.json` first." % f)
    login()
    print("NOTE: if you updated WF-B, restart the render service so the /claim lock clears:")
    print("      systemctl restart linkedin-render   (on the n8n host)\n")
    deploy("WF-A", "wf_a.json", "WF_A_ID")
    deploy("WF-B", "wf_b.json", "WF_B_ID")
