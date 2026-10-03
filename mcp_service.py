"""MCP 接入：Streamable-HTTP JSON-RPC 端点，让 Claude Code / Codex / Cursor 通过令牌操作面板。

无任何付费墙（原版把 MCP 放在闪电权益里）。
"""
import json

import store


MCP_TOKEN_KEY = "mcp_token"

TOOLS = [
    {
        "name": "list_accounts",
        "description": "列出面板里配置的所有云账号（多云平台，不返回任何密钥）",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "list_instances",
        "description": "列出某个账号的全部实例（含状态、IP、规格）",
        "inputSchema": {
            "type": "object",
            "properties": {"account_id": {"type": "integer", "description": "云账号ID"}},
            "required": ["account_id"],
        },
    },
    {
        "name": "instance_action",
        "description": "对实例执行电源操作：START / STOP / REBOOT / TERMINATE",
        "inputSchema": {
            "type": "object",
            "properties": {
                "account_id": {"type": "integer"},
                "instance_id": {"type": "string"},
                "action": {"type": "string", "enum": ["START", "STOP", "REBOOT", "TERMINATE"]},
            },
            "required": ["account_id", "instance_id", "action"],
        },
    },
    {
        "name": "list_ssh_sessions",
        "description": "列出面板里保存的 SSH 会话（主机列表）",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "ssh_exec",
        "description": "在某台 SSH 主机上执行一条命令并返回输出（私钥不出面板）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "integer"},
                "command": {"type": "string"},
            },
            "required": ["session_id", "command"],
        },
    },
]


def _text(payload):
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]}


def _dispatch(name, args):
    import sshpool
    import store

    if name == "list_accounts":
        rows = store.query("SELECT * FROM accounts WHERE platform='oci' ORDER BY id")
        return [{"id": a["id"], "platform": a["platform"], "name": a["name"],
                 "region": a["region"]} for a in rows]

    if name == "list_instances":
        rows = store.query("SELECT * FROM accounts WHERE id=? AND platform='oci'", (args["account_id"],))
        if not rows:
            raise ValueError("账号不存在")
        acct = rows[0]
        platform = acct["platform"]
        if platform == "oci":
            import oci_service
            return oci_service.list_instances(acct)
        raise ValueError("仅支持 Oracle Cloud")

    if name == "instance_action":
        rows = store.query("SELECT * FROM accounts WHERE id=? AND platform='oci'", (args["account_id"],))
        if not rows:
            raise ValueError("账号不存在")
        acct = rows[0]
        action = args["action"]
        import oci_service
        if action not in ("START", "STOP", "REBOOT", "TERMINATE"):
            raise ValueError("不支持的电源操作")
        real = {"STOP": "SOFTSTOP", "REBOOT": "SOFTRESET"}.get(action, action)
        return oci_service.manual_instance_action(acct, args["instance_id"], real)


    if name == "list_ssh_sessions":
        rows = store.query("SELECT id, name, host, port, username, tags FROM ssh_sessions ORDER BY id")
        return rows

    if name == "ssh_exec":
        rows = store.query("SELECT * FROM ssh_sessions WHERE id=?", (args["session_id"],))
        if not rows:
            raise ValueError("SSH 会话不存在")
        return {"output": sshpool.exec_batch([args["session_id"]], args["command"], timeout=30)}

    raise ValueError(f"未知工具 {name}")


def handle_rpc(body: dict):
    """处理一条 JSON-RPC 请求，返回 (status_code, payload_or_None)。"""
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0" or not isinstance(body.get("method"), str):
        return 400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}
    method = body.get("method", "")
    rid = body.get("id")

    if method == "initialize":
        return 200, {
            "jsonrpc": "2.0", "id": rid,
            "result": {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "oci-panel", "version": "2.1.0"},
            },
        }
    if method.startswith("notifications/"):
        return 202, None
    if method == "ping":
        return 200, {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "tools/list":
        return 200, {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = body.get("params") or {}
        try:
            out = _dispatch(params.get("name", ""), params.get("arguments") or {})
            return 200, {"jsonrpc": "2.0", "id": rid, "result": _text(out)}
        except Exception as e:
            return 200, {"jsonrpc": "2.0", "id": rid, "result": {**_text({"error": str(e)}), "isError": True}}
    return 200, {"jsonrpc": "2.0", "id": rid,
                 "error": {"code": -32601, "message": f"method not found: {method}"}}
