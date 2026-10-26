"""vSphere Automation API (vCenter only): the VMs carrying a tag.

Tags live in vCenter's REST API, not in the SOAP API pyvmomi speaks. Recent
vCenters (7.0 U2 and later) serve /api; older ones (6.7, 7.0) and govmomi's
vcsim serve /rest/com/vmware/... - both are tried, /api first.
"""
import base64
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request


class VapiError(Exception):
    pass


class Vapi:
    def __init__(self, host, port, user, password, insecure=True):
        self.base = f"https://{host}:{port}"
        self.ctx = ssl._create_unverified_context() if insecure else ssl.create_default_context()
        auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        self.sid, self.flavour = None, None
        for flavour, path in (("api", "/api/session"), ("rest", "/rest/com/vmware/cis/session")):
            try:
                v = self._call("POST", path, headers={"Authorization": auth})
            except urllib.error.HTTPError as e:
                if e.code in (404, 405):
                    continue
                raise VapiError(f"vSphere Automation API login: HTTP {e.code}")
            except OSError as e:
                raise VapiError(f"vSphere Automation API unreachable: {e}")
            self.sid, self.flavour = (v["value"] if isinstance(v, dict) else v), flavour
            return
        raise VapiError("no vSphere Automation API (tags need vCenter, not a standalone ESXi)")

    def _call(self, method, path, body=None, headers=None):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None)
        req.add_header("Content-Type", "application/json")
        if self.sid:
            req.add_header("vmware-api-session-id", self.sid)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        with urllib.request.urlopen(req, context=self.ctx, timeout=60) as r:
            raw = r.read()
            return json.loads(raw) if raw else None

    def _get(self, what, ident=None):
        """category/tag listing (ident None) or one object, in either flavour."""
        if self.flavour == "api":
            return self._call("GET", f"/api/cis/tagging/{what}" + (f"/{urllib.parse.quote(ident, safe='')}" if ident else ""))
        v = self._call("GET", f"/rest/com/vmware/cis/tagging/{what}" +
                       (f"/id:{urllib.parse.quote(ident, safe='')}" if ident else ""))
        return v["value"]

    def tag_ids(self, category, tag):
        cats = [c for c in self._get("category") if self._get("category", c)["name"] == category]
        if not cats:
            raise VapiError(f"no tag category {category!r}")
        ids = []
        for t in self._get("tag"):
            x = self._get("tag", t)
            if x["name"] == tag and x["category_id"] in cats:
                ids.append(t)
        if not ids:
            raise VapiError(f"no tag {tag!r} in category {category!r}")
        return ids

    def tagged_vms(self, category, tag):
        """Managed object IDs (vm-123) of the VMs carrying CATEGORY:TAG."""
        body = {"tag_ids": self.tag_ids(category, tag)}
        if self.flavour == "api":
            res = self._call("POST", "/api/cis/tagging/tag-association?action=list-attached-objects-on-tags", body)
        else:
            res = self._call("POST", "/rest/com/vmware/cis/tagging/tag-association?~action=list-attached-objects-on-tags",
                             body)["value"]
        return sorted({o["id"] for r in res for o in r.get("object_ids", []) if o.get("type") == "VirtualMachine"})

    def close(self):
        try:
            if self.flavour == "api":
                self._call("DELETE", "/api/session")
            elif self.flavour == "rest":
                self._call("DELETE", "/rest/com/vmware/cis/session")
        except OSError:
            pass
