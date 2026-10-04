#!/usr/bin/env python3
"""Matrix BD prototype on Operaton: configuration in, running application out.

  catalogue.json   predefined nodes (modules) with preset tasks, roles, users, views
  workspace.json   the live configuration the configurator edits (start: `reset`)

  python3 matrix.py reset              # workspace.json <- catalogue.json (Matrix defaults)
  python3 matrix.py publish            # validate, compile BPMN + forms, deploy release, sync users/roles/views
  python3 matrix.py check              # drive one site end to end as the real role users
  python3 matrix.py <op> '<json args>' # any configurator op, e.g. add_task '{"module": "legal", "task": {...}}'

mcp_server.py exposes the same ops as MCP tools. Stdlib only.
"""
import base64
import html
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.request
from xml.sax.saxutils import escape, quoteattr

HERE = os.path.dirname(os.path.abspath(__file__))
WS, CAT, OUT = (os.path.join(HERE, p) for p in ("workspace.json", "catalogue.json", "build"))
REST = os.environ.get("OPERATON_URL", "http://localhost:8080/engine-rest")
ADMIN = ("demo", os.environ.get("OPERATON_ADMIN_PASSWORD", "demo"))
TYPES = {"text", "textarea", "number", "date", "boolean", "select", "user", "file"}

# Operaton resource types (Authorization API)
APPLICATION, USER, GROUP, FILTER, PROC_DEF, TASK, PROC_INST, DEPLOYMENT = 0, 1, 2, 5, 6, 7, 8, 9


# ---------------------------------------------------------------- configuration

def load(path=WS):
    with open(path) as fh:
        return json.load(fh)


def save(cfg):
    validate(cfg)
    with open(WS, "w") as fh:
        json.dump(cfg, fh, indent=2)


def tasks_of(m):
    for s in m["tasks"]:
        yield from s["parallel"] if "parallel" in s else [s]


def all_tasks(cfg):
    return [(m, t) for m in cfg["modules"] for t in tasks_of(m)]


def field_registry(cfg):
    reg = {}
    for f in cfg["app"]["start"]["fields"] + [f for _, t in all_tasks(cfg) for f in t.get("fields", [])]:
        reg.setdefault(f["id"], f)
    return reg


def members(cfg, group):
    return [u["id"] for u in cfg["users"] if group in u["groups"]]


def outcomes(t):
    """Explicit outcomes, or the maker-checker default for kind=approval."""
    if "outcomes" in t:
        return t["outcomes"]
    if t.get("kind") != "approval":
        return []
    out = [{"id": "approve", "label": "Approve"}]
    if t.get("send_back"):
        out.append({"id": "send_back", "label": "Send back", "to": t["send_back"]})
    if t.get("allow_reject"):
        out.append({"id": "reject", "label": "Reject", "to": "@close"})
    return out


def validate(cfg):
    errs, reg = [], {}
    groups, mods = cfg["groups"], cfg["modules"]
    ident = re.compile(r"[A-Za-z0-9]+")
    for g in groups:
        if not ident.fullmatch(g):
            errs.append(f"group id '{g}' must be letters/digits only")
    for u in cfg["users"]:
        if not ident.fullmatch(u["id"]):
            errs.append(f"user id '{u['id']}' must be letters/digits only")
        errs += [f"user {u['id']}: unknown group '{g}'" for g in u["groups"] if g not in groups]

    def check_field(f, where):
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", f.get("id", "")):
            errs.append(f"{where}: bad field id {f.get('id')!r}")
            return
        if f.get("type") not in TYPES:
            errs.append(f"{where}.{f['id']}: type must be one of {sorted(TYPES)}")
        if f.get("type") == "select" and not f.get("options"):
            errs.append(f"{where}.{f['id']}: select needs options")
        if f.get("type") == "user" and f.get("group") not in groups:
            errs.append(f"{where}.{f['id']}: user field needs an existing group")
        prev = reg.setdefault(f["id"], f)
        if prev.get("type") != f.get("type"):
            errs.append(f"{where}.{f['id']}: already defined as {prev.get('type')}")

    for f in cfg["app"]["start"]["fields"]:
        check_field(f, "start")
    errs += [f"start role '{r}' is not a group" for r in cfg["app"]["start"]["roles"] if r not in groups]
    keys = [m["key"] for m in mods]
    if len(set(keys)) != len(keys):
        errs.append("duplicate module keys")
    seen = set()
    for m in mods:
        errs += [f"module {m['key']}: after unknown module '{a}'" for a in m.get("after", []) if a not in keys]
        if not m["tasks"]:
            errs.append(f"module {m['key']}: has no tasks")
        tids = {t["id"] for t in tasks_of(m)}
        par = {t["id"] for s in m["tasks"] if "parallel" in s for t in s["parallel"]}
        for t in tasks_of(m):
            where = f"{m['key']}.{t.get('id')}"
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", t.get("id", "")) or t["id"] in seen:
                errs.append(f"{where}: task id missing, invalid or duplicated")
            seen.add(t.get("id"))
            if t.get("role") not in groups:
                errs.append(f"{where}: role '{t.get('role')}' is not a group")
            elif not members(cfg, t["role"]):
                errs.append(f"{where}: nobody holds role '{t['role']}' (task could never be done)")
            for f in t.get("fields", []):
                check_field(f, where)
            branches = [o.get("to") for o in outcomes(t)] + [r.get("to") for r in t.get("route", [])]
            if t["id"] in par and branches:
                errs.append(f"{where}: tasks inside a parallel group cannot branch")
            for to in branches:
                if to not in (None, "next", "@close", "@end") and (to not in tids or to in par):
                    errs.append(f"{where}: target '{to}' is not a (non-parallel) task in this module")
    for m, t in all_tasks(cfg):
        a = t.get("assignee")
        if a and a != "initiator" and reg.get(a, {}).get("type") != "user":
            errs.append(f"{m['key']}.{t['id']}: assignee must be 'initiator' or a user-type field")
        errs += [f"{m['key']}.{t['id']}: shows unknown field '{s}'" for s in t.get("show", []) if s not in reg]
    # module graph must be acyclic
    after = {m["key"]: set(m.get("after", [])) & set(keys) for m in mods}
    while after:
        ready = [k for k, deps in after.items() if not deps]
        if not ready:
            errs.append(f"module order has a cycle among {sorted(after)}")
            break
        for k in ready:
            del after[k]
        for deps in after.values():
            deps.difference_update(ready)
    if errs:
        raise ValueError("invalid configuration:\n- " + "\n- ".join(errs))


# ---------------------------------------------------------------- BPMN generation

class E:
    """An end event created inline as a flow target."""
    def __init__(self, name, error=None, terminate=False, stage=None):
        self.name, self.error, self.terminate, self.stage = name, error, terminate, stage


def set_var(name, value):
    return "${execution.setVariable('%s', '%s')}" % (name, str(value).replace("'", "\\'"))


class Proc:
    def __init__(self, pid, name, starters=None, startable=True):
        self.pid, self.name, self.starters, self.startable = pid, name, starters, startable
        self.nodes, self.flows = {}, []

    def node(self, id_, kind, name="", **kw):
        assert id_ not in self.nodes, id_
        self.nodes[id_] = dict(kind=kind, name=name, **kw)
        return id_

    def flow(self, src, tgt, cond=None, name="", hint=None):
        if isinstance(tgt, E):
            listeners = [("start", set_var("stage", tgt.stage))] if tgt.stage else []
            tgt = self.node((hint or src) + "_end", "end", tgt.name, error=tgt.error, terminate=tgt.terminate,
                            listeners=listeners)
        self.flows.append(dict(id=f"f{len(self.flows) + 1}", src=src, tgt=tgt, cond=cond, name=name))

    def xml(self):
        for f in self.flows:
            assert f["src"] in self.nodes and f["tgt"] in self.nodes, f
        errors = sorted({n["error"] for n in self.nodes.values() if n.get("error")})
        body = [f'<bpmn:error id="Error_{c}" name="{c}" errorCode="{c}" />' for c in errors]
        attrs = f'id="{self.pid}" name={quoteattr(self.name)} isExecutable="true" operaton:historyTimeToLive="180"'
        if self.starters:
            attrs += f' operaton:candidateStarterGroups="{",".join(self.starters)}"'
        if not self.startable:
            attrs += ' operaton:isStartableInTasklist="false"'
        body.append(f"<bpmn:process {attrs}>")
        body += [self._node_xml(i, n) for i, n in self.nodes.items()]
        for f in self.flows:
            a = f'id="{f["id"]}" sourceRef="{f["src"]}" targetRef="{f["tgt"]}"'
            if f["name"]:
                a += f" name={quoteattr(f['name'])}"
            if f["cond"]:
                body.append(f'<bpmn:sequenceFlow {a}><bpmn:conditionExpression xsi:type="bpmn:tFormalExpression">'
                            f'{escape(f["cond"])}</bpmn:conditionExpression></bpmn:sequenceFlow>')
            else:
                body.append(f"<bpmn:sequenceFlow {a} />")
        body += ["</bpmn:process>", self._di()]
        return ('<?xml version="1.0" encoding="UTF-8"?>\n'
                '<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" '
                'xmlns:bpmndi="http://www.omg.org/spec/BPMN/20100524/DI" '
                'xmlns:dc="http://www.omg.org/spec/DD/20100524/DC" '
                'xmlns:di="http://www.omg.org/spec/DD/20100524/DI" '
                'xmlns:operaton="http://operaton.org/schema/1.0/bpmn" '
                'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
                f'id="defs_{self.pid}" targetNamespace="https://matrix-bd/workflow">\n'
                + "\n".join(body) + "\n</bpmn:definitions>\n")

    @staticmethod
    def _ext(n):
        inner = "".join(f'<operaton:executionListener event="{ev}" expression={quoteattr(ex)} />'
                        for ev, ex in n.get("listeners", []))
        if n["kind"] == "call":
            inner += ('<operaton:in businessKey="#{execution.processBusinessKey}" />'
                      '<operaton:in variables="all" /><operaton:out variables="all" />')
        return f"<bpmn:extensionElements>{inner}</bpmn:extensionElements>" if inner else ""

    def _node_xml(self, id_, n):
        name = f" name={quoteattr(n['name'])}" if n["name"] else ""
        k, ext = n["kind"], self._ext(n)
        if k == "start":
            form = f' operaton:formKey="embedded:deployment:{n["form"]}"' if n.get("form") else ""
            init = ' operaton:initiator="initiator"' if n.get("initiator") else ""
            return f'<bpmn:startEvent id="{id_}"{name}{form}{init}>{ext}</bpmn:startEvent>'
        if k == "end":
            inner = (f'<bpmn:errorEventDefinition errorRef="Error_{n["error"]}" />' if n.get("error")
                     else "<bpmn:terminateEventDefinition />" if n.get("terminate") else "")
            return f'<bpmn:endEvent id="{id_}"{name}>{ext}{inner}</bpmn:endEvent>'
        if k == "task":
            who = (f' operaton:assignee="{n["assignee"]}"' if n.get("assignee")
                   else f' operaton:candidateGroups="{n["group"]}"')
            return (f'<bpmn:userTask id="{id_}"{name} operaton:formKey="embedded:deployment:{n["form"]}"{who}>'
                    f"{ext}</bpmn:userTask>")
        if k == "xor":
            return f'<bpmn:exclusiveGateway id="{id_}"{name} />'
        if k == "par":
            return f'<bpmn:parallelGateway id="{id_}"{name} />'
        if k == "call":
            return (f'<bpmn:callActivity id="{id_}"{name} calledElement="{n["called"]}" '
                    f'operaton:calledElementBinding="deployment">{ext}</bpmn:callActivity>')
        if k == "boundary":
            return (f'<bpmn:boundaryEvent id="{id_}"{name} attachedToRef="{n["host"]}">'
                    f'<bpmn:errorEventDefinition errorRef="Error_{n["error"]}" /></bpmn:boundaryEvent>')
        raise ValueError(k)

    # auto layout: column = longest path from start, rows by branch order, loops drawn over the top
    SIZE = {"task": (110, 80), "call": (120, 80), "xor": (50, 50), "par": (50, 50),
            "start": (36, 36), "end": (36, 36), "boundary": (36, 36)}

    def _layout(self):
        succ = {i: [] for i in self.nodes}
        for f in self.flows:
            succ[f["src"]].append(f["tgt"])
        back, state = set(), {}

        def dfs(u):
            state[u] = 1
            for v in succ[u]:
                if state.get(v) == 1:
                    back.add((u, v))
                elif v not in state:
                    dfs(v)
            state[u] = 2

        order = list(self.nodes)
        for i in sorted(order, key=lambda i: self.nodes[i]["kind"] != "start"):
            if i not in state:
                dfs(i)
        fwd = [(f["src"], f["tgt"]) for f in self.flows if (f["src"], f["tgt"]) not in back]
        col = dict.fromkeys(order, 0)
        for _ in order:
            for u, v in fwd:
                col[v] = max(col[v], col[u] + 1)
            for i, n in self.nodes.items():
                if n["kind"] == "boundary":
                    col[i] = max(col[i], col[n["host"]])
        row, used = {}, set()

        def place(i, pref):
            r = pref
            while (col[i], r) in used:
                r += 1
            row[i] = r
            used.add((col[i], r))

        for u in sorted(order, key=lambda i: (col[i], order.index(i))):
            n = self.nodes[u]
            if n["kind"] == "boundary":
                row[u] = row[n["host"]]
            elif u not in row:
                place(u, 0)
            for k, v in enumerate([v for (a, v) in fwd if a == u]):
                if v not in row:
                    place(v, row[u] + (1 if n["kind"] == "boundary" else k))
        return col, row, back

    def _di(self):
        col, row, back = self._layout()
        box = {}
        for i, n in self.nodes.items():
            w, h = self.SIZE[n["kind"]]
            cx, cy = 120 + col[i] * 165, 120 + row[i] * 140
            if n["kind"] == "boundary":
                cx, cy = cx + 20, cy + 40
            box[i] = (cx - w / 2, cy - h / 2, w, h)
        out = [f'<bpmndi:BPMNDiagram id="dia_{self.pid}"><bpmndi:BPMNPlane id="plane_{self.pid}" bpmnElement="{self.pid}">']
        for i, (x, y, w, h) in box.items():
            out.append(f'<bpmndi:BPMNShape id="{i}_di" bpmnElement="{i}"><dc:Bounds x="{x:.0f}" y="{y:.0f}" '
                       f'width="{w}" height="{h}" /></bpmndi:BPMNShape>')
        loops = 0
        for f in self.flows:
            sx, sy, sw, sh = box[f["src"]]
            tx, ty, tw, th = box[f["tgt"]]
            scx, scy, tcx, tcy = sx + sw / 2, sy + sh / 2, tx + tw / 2, ty + th / 2
            if (f["src"], f["tgt"]) in back:
                top = min(scy, tcy) - 58 - 8 * (loops % 4)
                loops += 1
                pts = [(scx, sy), (scx, top), (tcx, top), (tcx, ty)]
            elif abs(scy - tcy) < 1:
                pts = [(sx + sw, scy), (tx, tcy)]
            elif tcy > scy:
                pts = [(scx, sy + sh), (scx, tcy), (tx, tcy)]
            else:
                pts = [(sx + sw, scy), (tcx, scy), (tcx, ty + th)]
            wps = "".join(f'<di:waypoint x="{x:.0f}" y="{y:.0f}" />' for x, y in pts)
            out.append(f'<bpmndi:BPMNEdge id="{f["id"]}_di" bpmnElement="{f["id"]}">{wps}</bpmndi:BPMNEdge>')
        out.append("</bpmndi:BPMNPlane></bpmndi:BPMNDiagram>")
        return "\n".join(out)


# ---------------------------------------------------------------- compiler: config -> processes + forms

def cond(c):
    """A rule: JUEL text over case data, or {"initiator_in": group}."""
    if isinstance(c, dict) and "initiator_in" in c:
        return ("execution.processEngineServices.identityService.createGroupQuery()"
                f".groupMember(initiator).groupId('{c['initiator_in']}').count() > 0")
    return c


def compile_module(cfg, m):
    p = Proc(f"{cfg['app']['key']}__{m['key']}", m["name"], startable=False)
    steps = m["tasks"]

    def entry(i):
        if i >= len(steps):
            return "done"
        s = steps[i]
        return f"split{i}" if "parallel" in s else (f"{s['id']}_skip" if s.get("skip_if") else s["id"])

    p.node("start", "start", "Start")
    p.flow("start", entry(0))
    p.node("done", "end", "Done")
    for i, s in enumerate(steps):
        nxt = entry(i + 1)
        if "parallel" in s:
            p.node(f"split{i}", "par")
            p.node(f"join{i}", "par")
            for t in s["parallel"]:
                p.flow(f"split{i}", t["id"])
                add_task(p, t, f"join{i}")
            p.flow(f"join{i}", nxt)
        else:
            add_task(p, s, nxt)
    return p


def add_task(p, t, nxt):
    tid = t["id"]

    def target(to):
        return {None: nxt, "next": nxt, "@end": "done"}.get(to, E("Closed", error="CLOSED") if to == "@close" else to)

    if t.get("skip_if"):
        c = cond(t["skip_if"])
        p.node(tid + "_skip", "xor", "Skip?")
        p.flow(tid + "_skip", tid, f"${{!({c})}}")
        p.flow(tid + "_skip", nxt, f"${{{c}}}", "Skip")
    a = t.get("assignee")
    p.node(tid, "task", t["name"], form=f"forms/{tid}.html", group=t["role"],
           assignee=None if not a else "${initiator}" if a == "initiator" else f"${{{a}}}")
    outs, routes = outcomes(t), t.get("route", [])
    if not outs and not routes:
        p.flow(tid, nxt)
        return
    p.node(tid + "_gw", "xor")
    p.flow(tid, tid + "_gw")
    for o in outs:
        p.flow(tid + "_gw", target(o.get("to")), f"${{{tid}_outcome == '{o['id']}'}}", o["label"], hint=f"{tid}_{o['id']}")
    if routes and not outs:
        conds = [cond(r["when"]) for r in routes]
        for i, (r, c) in enumerate(zip(routes, conds)):
            p.flow(tid + "_gw", target(r.get("to")), f"${{{c}}}", r.get("label", ""), hint=f"{tid}_r{i}")
        p.flow(tid + "_gw", nxt, "${" + " && ".join(f"!({c})" for c in conds) + "}", "Otherwise")


def compile_main(cfg):
    app, mods = cfg["app"], cfg["modules"]
    p = Proc(app["key"], app["name"], starters=app["start"]["roles"])
    p.node("start", "start", f"New {app['case_label'].lower()}", form="forms/start.html", initiator=True,
           listeners=[("start", set_var("stage", "Created"))])
    preds = {m["key"]: [a for a in m.get("after", [])] for m in mods}
    succs = {k: [m["key"] for m in mods if k in preds[m["key"]]] for k in preds}

    def inn(k):
        return f"join_{k}" if len(preds[k]) > 1 else f"call_{k}"

    def out(k):
        return f"split_{k}" if len(succs[k]) > 1 else f"call_{k}"

    roots = [k for k in preds if not preds[k]]
    sinks = [k for k in preds if not succs[k]]
    first = "start"
    if len(roots) > 1:
        first = p.node("split_start", "par")
        p.flow("start", first)
    for k in roots:
        p.flow(first, inn(k))
    for m in mods:
        k = m["key"]
        if len(preds[k]) > 1:
            p.node(f"join_{k}", "par")
            p.flow(f"join_{k}", f"call_{k}")
        p.node(f"call_{k}", "call", m["name"], called=f"{app['key']}__{k}", listeners=[
            ("start", set_var("stage", m["name"])), ("start", set_var(f"m_{k}", "in progress")),
            ("end", set_var(f"m_{k}", "done"))])
        p.node(f"closed_{k}", "boundary", "Closed", host=f"call_{k}", error="CLOSED")
        p.flow(f"closed_{k}", E(f"Closed in {m['name']}", terminate=True, stage=f"Closed in {m['name']}"))
        if len(succs[k]) > 1:
            p.node(f"split_{k}", "par")
            p.flow(f"call_{k}", f"split_{k}")
        for s in succs[k]:
            p.flow(out(k), inn(s))
    last = out(sinks[0])
    if len(sinks) > 1:
        last = p.node("join_end", "par")
        for s in sinks:
            p.flow(out(s), last)
    p.flow(last, E("Completed", stage="Completed"), hint="completed")
    return p


def widget(cfg, f):
    fid, label, typ = f["id"], html.escape(f["label"]), f["type"]
    a = f'cam-variable-name="{fid}" name="{fid}" id="{fid}"'
    req = " required" if f.get("required", typ != "boolean") else ""
    if typ == "boolean":
        return f'<div class="checkbox"><label><input type="checkbox" {a} cam-variable-type="Boolean"> {label}</label></div>'
    if typ in ("select", "user"):
        opts = ([(o, o) if isinstance(o, str) else tuple(o) for o in f["options"]] if typ == "select"
                else [(u["id"], u["name"]) for u in cfg["users"] if f["group"] in u["groups"]])
        inp = (f'<select class="form-control" {a} cam-variable-type="String"{req}><option value=""></option>'
               + "".join(f'<option value="{html.escape(v)}">{html.escape(n)}</option>' for v, n in opts) + "</select>")
    elif typ == "textarea":
        inp = f'<textarea class="form-control" rows="3" {a} cam-variable-type="String"{req}></textarea>'
    elif typ == "file":   # no `required`: Tasklist's AngularJS never marks a file input as filled
        inp = f'<input type="file" class="form-control" {a} cam-variable-type="File" cam-max-filesize="10000000">'
    else:
        kind = {"text": 'type="text"', "number": 'type="number" step="any"', "date": 'type="date"'}[typ]
        vt = "Double" if typ == "number" else "String"
        inp = f'<input {kind} class="form-control" {a} cam-variable-type="{vt}"{req}>'
    return f'<div class="form-group"><label class="control-label" for="{fid}">{label}</label>{inp}</div>'


SHOW_SCRIPT = """<script cam-script type="text/form-script">
(function () {
  var SHOW = %s, TID = '%s';
  var api = window.location.pathname.split('/app/')[0] + '/api/engine/engine/default';
  camForm.on('form-loaded', function () { SHOW.forEach(function (s) { camForm.variableManager.fetchVariable(s[0]); }); });
  camForm.on('variables-fetched', function () {
    var body = document.querySelector('#mx-show-' + TID + ' tbody');
    if (!body) { return; }
    body.innerHTML = '';
    SHOW.forEach(function (s) {
      var v = camForm.variableManager.variable(s[0]) || {}, td = document.createElement('td'), tr = document.createElement('tr');
      if (v.type === 'File' || (v.valueInfo && v.valueInfo.filename)) {
        var link = document.createElement('a');
        link.href = api + '/task/' + camForm.taskId + '/variables/' + s[0] + '/data';
        link.textContent = (v.valueInfo && v.valueInfo.filename) || 'download';
        link.target = '_blank';
        td.appendChild(link);
      } else {
        td.textContent = v.value === undefined || v.value === null || v.value === '' ? '-' : (v.value === true ? 'Yes' : v.value === false ? 'No' : String(v.value));
      }
      var th = document.createElement('th');
      th.textContent = s[1];
      th.style.width = '40%%';
      tr.appendChild(th); tr.appendChild(td); body.appendChild(tr);
    });
  });
})();
</script>"""


def form_html(cfg, reg, t=None):
    if t is None:   # start form
        return "<form role=\"form\">" + "".join(widget(cfg, f) for f in cfg["app"]["start"]["fields"]) + "</form>\n"
    mine = {f["id"] for f in t.get("fields", [])}
    show = [("initiator", "Created by")] + [(s, reg[s]["label"]) for s in cfg["app"]["header"] + t.get("show", [])
                                            if s in reg and s not in mine]
    show = list(dict.fromkeys(show))
    parts = ['<form role="form">',
             f'<div class="well well-sm" id="mx-show-{t["id"]}"><strong>{html.escape(cfg["app"]["case_label"])} data</strong>'
             '<table class="table table-condensed" style="margin:6px 0 0"><tbody></tbody></table></div>']
    parts += [widget(cfg, f) for f in t.get("fields", [])]
    outs = outcomes(t)
    if outs:
        parts.append(widget(cfg, {"id": f"{t['id']}_outcome", "label": "Decision", "type": "select",
                                  "options": [[o["id"], o["label"]] for o in outs]}))
    parts += [SHOW_SCRIPT % (json.dumps(show), t["id"]), "</form>\n"]
    return "\n".join(parts)


def build(cfg):
    """Write build/ and return {resource name: bytes} for one deployment (the release)."""
    validate(cfg)
    reg = field_registry(cfg)
    res = {f"{cfg['app']['key']}.bpmn": compile_main(cfg).xml(), "forms/start.html": form_html(cfg, reg)}
    for m in cfg["modules"]:
        res[f"{cfg['app']['key']}__{m['key']}.bpmn"] = compile_module(cfg, m).xml()
        for t in tasks_of(m):
            res[f"forms/{t['id']}.html"] = form_html(cfg, reg, t)
    for name, text in res.items():
        path = os.path.join(OUT, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(text)
    return {k: v.encode() for k, v in res.items()}


# ---------------------------------------------------------------- Operaton REST

class ApiError(Exception):
    def __init__(self, code, msg):
        super().__init__(f"{code}: {msg}")
        self.code = code


def api(method, path, body=None, raw=None, headers=None, auth=ADMIN):
    data = raw if raw is not None else json.dumps(body).encode() if body is not None else None
    hdrs = {"Authorization": "Basic " + base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()}
    hdrs.update(headers or ({"Content-Type": "application/json"} if data else {}))
    req = urllib.request.Request(REST + path, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req) as r:
            txt = r.read()
            return json.loads(txt) if txt else None
    except urllib.error.HTTPError as e:
        raise ApiError(e.code, e.read().decode()[:500]) from None


def deploy(cfg, files):
    b = "matrixrelease"
    parts = [("deployment-name", f"{cfg['app']['key']}-release"), ("enable-duplicate-filtering", "true"),
             ("deployment-source", "matrix-configurator")]
    raw = b"".join(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k, v in parts)
    for name, content in files.items():
        ctype = "application/xml" if name.endswith(".bpmn") else "text/html"
        raw += (f'--{b}\r\nContent-Disposition: form-data; name="{name}"; filename="{name}"\r\n'
                f"Content-Type: {ctype}\r\n\r\n").encode() + content + b"\r\n"
    raw += f"--{b}--\r\n".encode()
    return api("POST", "/deployment/create", raw=raw, headers={"Content-Type": f"multipart/form-data; boundary={b}"})


def grant(perms, rtype, rid, group=None, user=None):
    """Idempotently ensure a GRANT (or global, for user '*') authorization holds these permissions."""
    who = f"groupIdIn={group}" if group else f"userIdIn={user}"
    for a in api("GET", f"/authorization?resourceType={rtype}&resourceId={rid}&{who}"):
        if set(perms) - set(a["permissions"]):
            api("PUT", f"/authorization/{a['id']}", {**a, "permissions": sorted(set(a["permissions"]) | set(perms))})
        return
    api("POST", "/authorization/create", {"type": 0 if user == "*" else 1, "permissions": perms, "userId": user,
                                          "groupId": group, "resourceType": rtype, "resourceId": rid})


def sync_identity(cfg):
    groups = cfg["groups"]
    have = {g["id"] for g in api("GET", "/group?maxResults=1000")}
    for gid, g in groups.items():
        body = {"id": gid, "name": g["name"], "type": "WORKFLOW"}
        api("PUT", f"/group/{gid}", body) if gid in have else api("POST", "/group/create", body)
    users = {u["id"]: u for u in api("GET", "/user?maxResults=1000")}
    for u in cfg["users"]:
        first, _, last = u["name"].partition(" ")
        profile = {"id": u["id"], "firstName": first, "lastName": last or "-", "email": f"{u['id']}@matrix.local"}
        if u["id"] in users:
            api("PUT", f"/user/{u['id']}/profile", profile)
            api("PUT", f"/user/{u['id']}/credentials", {"password": u["password"], "authenticatedUserPassword": ADMIN[1]})
        else:
            api("POST", "/user/create", {"profile": profile, "credentials": {"password": u["password"]}})
        current = {g["id"] for g in api("GET", f"/group?member={u['id']}")}
        for g in set(u["groups"]) - current:
            api("PUT", f"/group/{g}/members/{u['id']}")
        for g in (current & set(groups)) - set(u["groups"]):
            api("DELETE", f"/group/{g}/members/{u['id']}")
    wanted = {u["id"] for u in cfg["users"]}
    for uid, u in users.items():   # users this configurator created but that were removed from the config
        if u.get("email", "").endswith("@matrix.local") and uid not in wanted:
            api("DELETE", f"/user/{uid}")

    # permissions: everyone may read definitions/forms and names; roles get their apps; starters may start cases;
    # cockpit roles (admin, observer) get read-only insight into every case. Task access comes from assignment.
    for rtype, perms in ((PROC_DEF, ["READ"]), (DEPLOYMENT, ["READ"]), (GROUP, ["READ"]), (USER, ["READ"])):
        grant(perms, rtype, "*", user="*")
    for gid, g in groups.items():
        for app in g.get("apps", []):
            grant(["ACCESS"], APPLICATION, app, group=gid)
        if "cockpit" in g.get("apps", []):
            grant(["READ", "READ_INSTANCE", "READ_HISTORY", "READ_TASK"], PROC_DEF, "*", group=gid)
            grant(["READ"], PROC_INST, "*", group=gid)
            grant(["READ"], TASK, "*", group=gid)
    for gid in cfg["app"]["start"]["roles"]:
        grant(["READ", "CREATE_INSTANCE"], PROC_DEF, cfg["app"]["key"], group=gid)
        grant(["CREATE"], PROC_INST, "*", group=gid)


def sync_views(cfg):
    reg = field_registry(cfg)
    have = {f["name"]: f for f in api("GET", "/filter?resourceType=Task")}
    for i, v in enumerate(cfg["views"]):
        body = {"resourceType": "Task", "name": v["name"], "query": v["query"], "properties": {
            "priority": i - 50, "refresh": True,
            "variables": [{"name": c, "label": reg[c]["label"] if c in reg else c.capitalize()} for c in v["columns"]]}}
        if v["name"] in have:
            fid = have[v["name"]]["id"]
            api("PUT", f"/filter/{fid}", body)
        else:
            fid = api("POST", "/filter/create", body)["id"]
        if v["audience"] == "*":
            grant(["READ"], FILTER, fid, user="*")
        else:
            for g in v["audience"]:
                grant(["READ"], FILTER, fid, group=g)


# ---------------------------------------------------------------- configurator operations (CLI + MCP)

def _module(cfg, key):
    for m in cfg["modules"]:
        if m["key"] == key:
            return m
    raise ValueError(f"no module '{key}'; modules: {[m['key'] for m in cfg['modules']]}")


def _locate(m, task_id):
    for s in m["tasks"]:
        group = s["parallel"] if "parallel" in s else [s]
        for i, t in enumerate(group):
            if t["id"] == task_id:
                return group, i
    raise ValueError(f"no task '{task_id}' in module '{m['key']}'")


def op_reset():
    """Replace the workspace with the Matrix defaults from the catalogue (fresh demo password)."""
    cfg = load(CAT)
    password = f"Matrix-{secrets.token_hex(3)}"
    for u in cfg["users"]:
        u["password"] = password
    save(cfg)
    return f"workspace reset to Matrix defaults: {len(cfg['modules'])} modules, {len(cfg['users'])} users"


def op_show():
    """Summary of the live configuration: module order, tasks, roles, users."""
    cfg = load()
    lines = [f"{cfg['app']['name']}  (started by {', '.join(cfg['app']['start']['roles'])})"]
    for m in cfg["modules"]:
        lines.append(f"\n[{m['key']}] {m['name']}  after: {', '.join(m.get('after', [])) or 'start'}")
        for s in m["tasks"]:
            for t in s["parallel"] if "parallel" in s else [s]:
                extra = [f"assignee={t['assignee']}"] if t.get("assignee") else []
                extra += [f"fields={[f['id'] for f in t.get('fields', [])]}"] if t.get("fields") else []
                extra += [f"outcomes={[(o['id'], o.get('to', 'next')) for o in outcomes(t)]}"] if outcomes(t) else []
                extra += [f"route={t['route']}"] if t.get("route") else []
                extra += [f"skip_if={t['skip_if']}"] if t.get("skip_if") else []
                lines.append(f"  {'||' if 'parallel' in s else '-'} {t['id']}: {t['name']} [{t['role']}] {' '.join(extra)}")
    lines.append("\nusers: " + ", ".join(f"{u['id']}({'/'.join(u['groups'])})" for u in cfg["users"]))
    return "\n".join(lines)


def op_catalogue():
    """Predefined nodes (modules) available to add, with their preset tasks."""
    return "\n".join(f"{m['key']}: {m['name']} -> " + ", ".join(t["id"] for t in tasks_of(m)) for m in load(CAT)["modules"])


def op_add_module(key: str, name: str | None = None, after: list[str] | None = None, from_catalogue: str | None = None, tasks: list[dict] | None = None):
    """Add a module: copy a predefined node (from_catalogue) or define a new one with tasks."""
    cfg = load()
    if from_catalogue:
        m = json.loads(json.dumps(_module(load(CAT), from_catalogue)))
        m["key"] = key
    else:
        m = {"key": key, "name": name or key, "tasks": tasks or []}
    if name:
        m["name"] = name
    if after is not None:
        m["after"] = after
    cfg["modules"].append(m)
    save(cfg)
    return f"added module {key} after {m.get('after', [])}"


def op_remove_module(key: str):
    """Remove a module; modules that came after it now come after its predecessors."""
    cfg = load()
    m = _module(cfg, key)
    cfg["modules"].remove(m)
    for other in cfg["modules"]:
        if key in other.get("after", []):
            other["after"] = list(dict.fromkeys([a for a in other["after"] if a != key] + m.get("after", [])))
    save(cfg)
    return f"removed module {key}"


def op_set_after(key: str, after: list[str]):
    """Set which modules must complete before this one starts (all of them = parallel join)."""
    cfg = load()
    _module(cfg, key)["after"] = after
    save(cfg)
    return f"{key} now starts after {after or 'start'}"


def op_add_task(module: str, task: dict, before: str | None = None):
    """Insert a task into a module (before an existing task id, else at the end)."""
    cfg = load()
    m = _module(cfg, module)
    if before:
        group, i = _locate(m, before)
        group.insert(i, task)
    else:
        m["tasks"].append(task)
    save(cfg)
    return f"added task {task.get('id')} to {module}"


def op_update_task(module: str, task_id: str, changes: dict):
    """Merge changes into a task; a null value removes that key."""
    cfg = load()
    group, i = _locate(_module(cfg, module), task_id)
    t = group[i]
    for k, v in changes.items():
        t.pop(k, None) if v is None else t.__setitem__(k, v)
    save(cfg)
    return f"updated {module}.{task_id}"


def op_remove_task(module: str, task_id: str):
    """Remove a task from a module."""
    cfg = load()
    m = _module(cfg, module)
    group, i = _locate(m, task_id)
    del group[i]
    m["tasks"] = [s for s in m["tasks"] if "parallel" not in s or s["parallel"]]
    save(cfg)
    return f"removed {module}.{task_id}"


def op_add_field(module: str, task_id: str, field: dict):
    """Add a data field to a task (types: text, textarea, number, date, boolean, select, user, file)."""
    cfg = load()
    group, i = _locate(_module(cfg, module), task_id)
    group[i].setdefault("fields", []).append(field)
    save(cfg)
    return f"added field {field.get('id')} to {module}.{task_id}"


def op_add_group(id: str, name: str, apps: list[str] | None = None):
    """Add a role (group). apps: tasklist, cockpit, admin."""
    cfg = load()
    cfg["groups"][id] = {"name": name, "apps": apps or ["tasklist"]}
    save(cfg)
    return f"added role {id}"


def op_add_user(id: str, name: str, groups: list[str], password: str | None = None):
    """Add or replace a user with roles; password defaults to the shared demo password."""
    cfg = load()
    cfg["users"] = [u for u in cfg["users"] if u["id"] != id]
    cfg["users"].append({"id": id, "name": name, "groups": groups,
                         "password": password or next((u["password"] for u in cfg["users"]), f"Matrix-{secrets.token_hex(3)}")})
    save(cfg)
    return f"user {id} has roles {groups}"


def op_remove_user(id: str):
    """Remove a user (deleted from Operaton on next publish)."""
    cfg = load()
    cfg["users"] = [u for u in cfg["users"] if u["id"] != id]
    save(cfg)
    return f"removed user {id}"


def op_add_view(name: str, query: dict, columns: list[str], audience: str | list[str]):
    """Add or replace a Tasklist view. audience: '*' or list of roles."""
    cfg = load()
    cfg["views"] = [v for v in cfg["views"] if v["name"] != name] + [
        {"name": name, "query": query, "columns": columns, "audience": audience}]
    save(cfg)
    return f"view '{name}' saved"


def op_publish():
    """Validate, compile and deploy a new release; sync users, roles, permissions and views."""
    cfg = load()
    res = deploy(cfg, build(cfg))
    sync_identity(cfg)
    sync_views(cfg)
    defs = res.get("deployedProcessDefinitions") or {}
    versions = sorted({d["version"] for d in defs.values()})
    return (f"published release {res['id']} ({len(defs)} processes, version {versions or 'unchanged'}); "
            f"{len(cfg['users'])} users synced. Tasklist: http://localhost:8080/operaton/app/tasklist/")


def op_migrate_running():
    """Move every running case (and module instance) onto the latest published version."""
    cfg, out = load(), []
    keys = [cfg["app"]["key"]] + [f"{cfg['app']['key']}__{m['key']}" for m in cfg["modules"]]
    for key in keys:
        defs = api("GET", f"/process-definition?key={key}&sortBy=version&sortOrder=desc")
        if not defs:
            continue
        for old in defs[1:]:
            n = api("GET", f"/process-instance/count?processDefinitionId={old['id']}")["count"]
            if not n:
                continue
            try:
                plan = api("POST", "/migration/generate", {"sourceProcessDefinitionId": old["id"],
                                                          "targetProcessDefinitionId": defs[0]["id"],
                                                          "updateEventTriggers": True})
                api("POST", "/migration/execute", {"migrationPlan": plan,
                                                   "processInstanceQuery": {"processDefinitionId": old["id"]}})
                out.append(f"{key}: moved {n} from v{old['version']} to v{defs[0]['version']}")
            except ApiError as e:
                out.append(f"{key}: could not move v{old['version']} ({e})")
    return "\n".join(out) or "nothing to migrate"


def op_start_case(user: str, values: dict):
    """Start a case as a user (they must hold a start role). values: start-form fields."""
    cfg = load()
    u = next(u for u in cfg["users"] if u["id"] == user)
    key = f"SITE-{int(time.time() * 1000) % 10**8}"
    api("POST", f"/process-definition/key/{cfg['app']['key']}/start",
        {"businessKey": key, "variables": {k: {"value": v} for k, v in values.items()}}, auth=(user, u["password"]))
    return f"started {key} as {user}"


def op_case_status(business_key: str):
    """Where a case is: stage, module states, and open tasks with who holds them."""
    cfg = load()
    insts = api("GET", f"/history/process-instance?processInstanceBusinessKey={business_key}"
                       f"&processDefinitionKey={cfg['app']['key']}")
    if not insts:
        return f"no case {business_key}"
    inst = insts[0]
    vars_ = {v["name"]: v["value"] for v in
             api("GET", f"/history/variable-instance?processInstanceId={inst['id']}")}
    lines = [f"{business_key}: {inst['state']}, stage={vars_.get('stage')}"]
    lines += [f"  {m['key']}: {vars_.get('m_' + m['key'], 'not started')}" for m in cfg["modules"]]
    for t in api("GET", f"/task?processInstanceBusinessKey={business_key}"):
        links = [l["groupId"] for l in api("GET", f"/task/{t['id']}/identity-links") if l.get("groupId")]
        lines.append(f"  open: {t['name']} -> {t['assignee'] or 'queue ' + ','.join(links)}")
    return "\n".join(lines)


# ---------------------------------------------------------------- end-to-end check as the real users

def check():
    """Start a site as a BD executive and complete every task as an eligible user, asserting role scoping."""
    cfg = load()
    users = {u["id"]: u for u in cfg["users"]}
    tdef = {t["id"]: t for _, t in all_tasks(cfg)}
    readers = {u for u in users if any("cockpit" in cfg["groups"][g].get("apps", []) for g in users[u]["groups"])}
    auth = lambda uid: (uid, users[uid]["password"])  # noqa: E731
    script = {"legal_dd_verdict": ["negative"], "legal_change_request": ["raise"], "legal_licensing": [{"licFireNoc": False}],
              "design_recce_review": ["send_back"], "fc_admin": ["send_back"]}

    def value(f):
        t = f["type"]
        if t == "file":
            return {"value": base64.b64encode(b"check").decode(), "type": "File",
                    "valueInfo": {"filename": f"{f['id']}.txt", "mimeType": "text/plain"}}
        return {"value": {"number": 1000.0, "boolean": True, "date": "2026-10-04",
                          "select": (f.get("options") or [""])[0],
                          "user": (members(cfg, f.get("group", "")) or [""])[0]}.get(t, "Check")}

    starter = members(cfg, cfg["app"]["start"]["roles"][0])[0]
    key = f"CHECK-{int(time.time())}"
    api("POST", f"/process-definition/key/{cfg['app']['key']}/start", {"businessKey": key, "variables": {
        f["id"]: value(f) for f in cfg["app"]["start"]["fields"]}}, auth=auth(starter))
    done, guest_blocked = [], False
    for _ in range(400):
        tasks = api("GET", f"/task?processInstanceBusinessKey={key}")
        if not tasks:
            break
        for t in tasks:
            td = tdef[t["taskDefinitionKey"]]
            actor = t["assignee"] or members(cfg, td["role"])[0]
            mine = api("GET", f"/task?processInstanceBusinessKey={key}", auth=auth(actor))
            assert any(x["id"] == t["id"] for x in mine), f"{actor} cannot see their task {td['id']}"
            strangers = [u for u in users if u != actor and u not in readers
                         and (td.get("assignee") or td["role"] not in users[u]["groups"])]
            if strangers:
                theirs = api("GET", f"/task?processInstanceBusinessKey={key}", auth=auth(strangers[0]))
                assert all(x["id"] != t["id"] for x in theirs), f"{strangers[0]} can see {td['id']}"
            if readers and not guest_blocked:
                try:
                    api("POST", f"/task/{t['id']}/complete", {}, auth=auth(sorted(readers)[-1]))
                    raise AssertionError("read-only user completed a task")
                except ApiError as e:
                    guest_blocked = e.code in (403, 500)
            body = {f["id"]: value(f) for f in td.get("fields", [])}
            if outcomes(td):
                body[f"{td['id']}_outcome"] = {"value": outcomes(td)[0]["id"]}
            if script.get(td["id"]):
                step = script[td["id"]].pop(0)
                body.update({k: {"value": v} for k, v in step.items()} if isinstance(step, dict)
                            else {f"{td['id']}_outcome": {"value": step}})
            api("POST", f"/task/{t['id']}/complete", {"variables": body}, auth=auth(actor))
            done.append((td["id"], actor))
    inst = api("GET", f"/history/process-instance?processInstanceBusinessKey={key}&processDefinitionKey={cfg['app']['key']}")[0]
    ends = [a["activityId"] for a in api("GET", f"/history/activity-instance?processInstanceId={inst['id']}&activityType=noneEndEvent")]
    assert inst["state"] == "COMPLETED" and ends == ["completed_end"], (inst["state"], ends)
    assert guest_blocked, "read-only user was not blocked"
    print(f"OK {key}: {len(done)} tasks done by {len({a for _, a in done})} different users; "
          "every task hidden from other roles; read-only user blocked")

    # rule check: a site created by a supervisor skips draft review
    rule = next((t for _, t in all_tasks(cfg) if t.get("skip_if") == {"initiator_in": "bdSupervisor"}), None)
    if rule and members(cfg, "bdSupervisor"):
        sup, key2 = members(cfg, "bdSupervisor")[0], f"CHECK-{int(time.time())}-sup"
        inst2 = api("POST", f"/process-definition/key/{cfg['app']['key']}/start", {"businessKey": key2, "variables": {
            f["id"]: value(f) for f in cfg["app"]["start"]["fields"]}}, auth=auth(sup))
        first = [t["taskDefinitionKey"] for t in api("GET", f"/task?processInstanceBusinessKey={key2}")]
        api("DELETE", f"/process-instance/{inst2['id']}")
        assert rule["id"] not in first, first
        print(f"OK supervisor-created site skipped '{rule['id']}', first task: {first}")


OPS = {name[3:]: fn for name, fn in globals().items() if name.startswith("op_")}

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "show"
    if cmd == "check":
        check()
    elif cmd == "build":
        print(f"built {len(build(load()))} resources into {OUT}")
    else:
        print(OPS[cmd](**(json.loads(sys.argv[2]) if len(sys.argv) > 2 else {})))
