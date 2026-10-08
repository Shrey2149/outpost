"""MCP configurator for the Matrix-on-Operaton prototype. Run: uv run --with "mcp<2" python mcp_server.py"""
import contextlib
import io

from mcp.server.fastmcp import FastMCP

import matrix

mcp = FastMCP("matrix-configurator", instructions="""
Configures a Matrix-style case application running on Operaton. Edits go to workspace.json and are
validated on every change; nothing is live until `publish` (which creates a new immutable release;
running cases stay on the version they started with).

Model:
- module: {key, name, after: [module keys that must all finish first], tasks: [task | {"parallel": [task, ...]}]}
- task: {id, name, role (group id), assignee?: "initiator" | <user-type field id>, kind?: "approval",
  fields?: [field], show?: [field ids from any module, shown read-only],
  outcomes?: [{id, label, to?: task id | "@close" (ends the case) | "@end" (finish module)}]  (default: next task),
  send_back?: task id (approval shorthand), allow_reject?: bool,
  route?: [{when: JUEL over field ids, to: task id, label}], skip_if?: JUEL or {"initiator_in": group}}
- field: {id, label, type: text|textarea|number|date|boolean|select|user|file, options? (select), group? (user), required?}
Typical loop: show -> catalogue -> add_module/add_task/add_field/add_user -> publish -> start_case -> case_status.
""")

for name, fn in matrix.OPS.items():
    mcp.tool(name=name)(fn)


@mcp.tool()
def check() -> str:
    """Drive one case end to end as the real role users and assert role scoping (needs a published release)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        matrix.check()
    return out.getvalue()


if __name__ == "__main__":
    mcp.run()
