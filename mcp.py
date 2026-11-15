# mcp.py — MCP at /mcp: JSON-RPC 2.0 Streamable HTTP, revision 2026-07-28.
#
# The kit's client connects modern-first: server/discover, then tools/list
# and tools/call. server/discover carries the identity twice — in
# _meta['io.modelcontextprotocol/serverInfo'] (read first) and in serverInfo —
# plus supportedVersions and capabilities.tools. tools/list returns a fixed
# tool array, so two calls are byte-identical. Tool results carry both a
# text content block (JSON) and structuredContent; list_issues' text holds
# the issue title / "number": N the check looks for. Unknown tools and
# missing required args are JSON-RPC -32602 / isError, never HTTP 500.
# Auth (401 without a token) is enforced by server.py before this runs.

import json

REVISION = "2026-07-28"
SERVER_NAME = "forge"
SERVER_VERSION = "1.0.0"

SERVER_INFO = {"name": SERVER_NAME, "version": SERVER_VERSION}


def _object_schema(properties, required):
    return {"type": "object", "properties": properties, "required": required}


_TOOLS = [
    {
        "name": "create_repo",
        "description": "Create a repository owned by the viewer.",
        "inputSchema": _object_schema({
            "name": {"type": "string", "description": "Repository name"},
            "description": {"type": "string", "description": "Repository description"},
            "private": {"type": "boolean", "description": "Whether the repository is private"},
        }, ["name"]),
    },
    {
        "name": "get_repo",
        "description": "Get a repository by owner and name.",
        "inputSchema": _object_schema({
            "owner": {"type": "string", "description": "Repository owner login"},
            "repo": {"type": "string", "description": "Repository name"},
        }, ["owner", "repo"]),
    },
    {
        "name": "create_issue",
        "description": "Create an issue in a repository.",
        "inputSchema": _object_schema({
            "owner": {"type": "string", "description": "Repository owner login"},
            "repo": {"type": "string", "description": "Repository name"},
            "title": {"type": "string", "description": "Issue title"},
            "body": {"type": "string", "description": "Issue body"},
        }, ["owner", "repo", "title"]),
    },
    {
        "name": "list_issues",
        "description": "List issues in a repository.",
        "inputSchema": _object_schema({
            "owner": {"type": "string", "description": "Repository owner login"},
            "repo": {"type": "string", "description": "Repository name"},
            "state": {"type": "string", "description": "Issue state: open, closed or all"},
        }, ["owner", "repo"]),
    },
    {
        "name": "create_pull_request",
        "description": "Create a pull request in a repository.",
        "inputSchema": _object_schema({
            "owner": {"type": "string", "description": "Repository owner login"},
            "repo": {"type": "string", "description": "Repository name"},
            "title": {"type": "string", "description": "Pull request title"},
            "head": {"type": "string", "description": "Head branch name"},
            "base": {"type": "string", "description": "Base branch name"},
        }, ["owner", "repo", "title", "head", "base"]),
    },
    {
        "name": "merge_pull_request",
        "description": "Merge a pull request with a squash merge.",
        "inputSchema": _object_schema({
            "owner": {"type": "string", "description": "Repository owner login"},
            "repo": {"type": "string", "description": "Repository name"},
            "pull_number": {"type": "integer", "description": "Pull request number"},
        }, ["owner", "repo", "pull_number"]),
    },
]

_TOOL_NAMES = {t["name"] for t in _TOOLS}


def _result(payload):
    text = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return {"content": [{"type": "text", "text": text}],
            "structuredContent": payload}


def _error_result(message):
    return {"content": [{"type": "text", "text": message}],
            "structuredContent": {"error": message},
            "isError": True}


def _get_repo(store, args):
    owner = args.get("owner")
    repo_name = args.get("repo")
    if store.viewer_login is not None and owner != store.viewer_login:
        return None
    return store.repos.get(repo_name)


def _call_tool(store, name, args):
    """Returns (payload_dict, is_error)."""
    if name == "create_repo":
        repo_name = args.get("name")
        if not repo_name:
            return {"error": "missing required argument: name"}, True
        if repo_name in store.repos:
            return {"error": "repository already exists: %s" % repo_name}, True
        repo = store.create_repo(repo_name, private=bool(args.get("private", False)),
                                 description=args.get("description"))
        owner = store.viewer_login or ""
        return {"name": repo.name,
                "full_name": "%s/%s" % (owner, repo.name) if owner else repo.name,
                "private": repo.private,
                "default_branch": repo.default_branch,
                "description": repo.description,
                "id": repo.database_id}, False
    if name == "get_repo":
        repo = _get_repo(store, args)
        if repo is None:
            return {"error": "repository not found"}, True
        owner = store.viewer_login or ""
        return {"name": repo.name,
                "full_name": "%s/%s" % (owner, repo.name) if owner else repo.name,
                "private": repo.private,
                "default_branch": repo.default_branch,
                "description": repo.description,
                "id": repo.database_id}, False
    if name == "create_issue":
        repo = _get_repo(store, args)
        if repo is None:
            return {"error": "repository not found"}, True
        title = args.get("title")
        if not title:
            return {"error": "missing required argument: title"}, True
        status, result = store.create_issue(repo, title, args.get("body") or "")
        if status != 201:
            return {"error": result.get("message", "Validation Failed")}, True
        return {"number": result.number, "title": result.title,
                "body": result.body, "state": result.state,
                "id": result.gid}, False
    if name == "list_issues":
        repo = _get_repo(store, args)
        if repo is None:
            return {"error": "repository not found"}, True
        state = str(args.get("state") or "open").lower()
        issues = [repo.issues[k] for k in sorted(repo.issues)]
        if state == "open":
            issues = [i for i in issues if i.state == "open"]
        elif state == "closed":
            issues = [i for i in issues if i.state == "closed"]
        return {"issues": [{"number": i.number, "title": i.title,
                            "body": i.body, "state": i.state,
                            "id": i.gid} for i in issues]}, False
    if name == "create_pull_request":
        repo = _get_repo(store, args)
        if repo is None:
            return {"error": "repository not found"}, True
        title = args.get("title")
        head = args.get("head")
        base = args.get("base")
        if not title:
            return {"error": "missing required argument: title"}, True
        if not head:
            return {"error": "missing required argument: head"}, True
        if not base:
            return {"error": "missing required argument: base"}, True
        status, result = store.create_pull(repo, head, base, title,
                                           args.get("body") or "")
        if status != 201:
            return {"error": result.get("message", "Validation Failed")}, True
        return {"number": result.number, "title": result.title,
                "state": result.state, "head": result.head, "base": result.base,
                "id": result.gid}, False
    if name == "merge_pull_request":
        repo = _get_repo(store, args)
        if repo is None:
            return {"error": "repository not found"}, True
        number = args.get("pull_number")
        if number is None:
            return {"error": "missing required argument: pull_number"}, True
        try:
            number = int(number)
        except (TypeError, ValueError):
            return {"error": "pull_number must be an integer"}, True
        pr = repo.pull_by_number(number)
        if pr is None:
            return {"error": "pull request not found"}, True
        status, result = store.merge_pull(repo, pr, "squash")
        if status != 200:
            return {"error": result.get("message", "error")}, True
        return {"number": pr.number, "merged": True, "state": pr.state,
                "sha": result.get("sha")}, False
    return {"error": "unknown tool: %s" % name}, True


def _rpc_error(req_id, code, message):
    return {"jsonrpc": "2.0", "id": req_id,
            "error": {"code": code, "message": message}}


def _rpc_result(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def handle_mcp(store, body):
    """body: raw request bytes.
    Returns (status, json_object_or_None). None means "no response"
    (a notification)."""
    try:
        request = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        # Parse error: JSON-RPC -32700, id null, HTTP 400.
        return 400, _rpc_error(None, -32700, "Parse error")
    if not isinstance(request, dict):
        return 400, _rpc_error(None, -32700, "Parse error")
    method = request.get("method")
    params = request.get("params") or {}
    req_id = request.get("id")
    is_notification = "id" not in request
    if not isinstance(params, dict):
        params = {}

    if method == "server/discover":
        result = {
            "supportedVersions": [REVISION],
            "capabilities": {"tools": {}},
            "serverInfo": dict(SERVER_INFO),
            "_meta": {"io.modelcontextprotocol/serverInfo": dict(SERVER_INFO)},
        }
        if is_notification:
            return 202, None
        return 200, _rpc_result(req_id, result)

    if method == "initialize":
        result = {
            "protocolVersion": REVISION,
            "capabilities": {"tools": {}},
            "serverInfo": dict(SERVER_INFO),
            "_meta": {"io.modelcontextprotocol/serverInfo": dict(SERVER_INFO)},
        }
        if is_notification:
            return 202, None
        return 200, _rpc_result(req_id, result)

    if method == "notifications/initialized":
        return 202, None

    if method == "ping":
        if is_notification:
            return 202, None
        return 200, _rpc_result(req_id, {})

    if method == "tools/list":
        if is_notification:
            return 202, None
        # The fixed array: byte-identical across calls.
        return 200, _rpc_result(req_id, {"tools": _TOOLS})

    if method == "tools/call":
        if is_notification:
            return 202, None
        tool_name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            args = {}
        if tool_name not in _TOOL_NAMES:
            return 200, _rpc_error(req_id, -32602, "unknown tool: %s" % tool_name)
        # Missing required args -> isError in the tool result.
        tool = next(t for t in _TOOLS if t["name"] == tool_name)
        required = tool["inputSchema"].get("required", [])
        missing = [r for r in required if r not in args or args[r] is None]
        if missing:
            return 200, _rpc_result(req_id, _error_result(
                "missing required argument: %s" % missing[0]))
        payload, is_error = _call_tool(store, tool_name, args)
        if is_error:
            return 200, _rpc_result(req_id, _error_result(
                payload.get("error", "tool error")))
        return 200, _rpc_result(req_id, _result(payload))

    if is_notification:
        return 202, None
    return 200, _rpc_error(req_id, -32601, "method not found: %s" % method)
