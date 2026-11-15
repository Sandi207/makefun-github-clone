# graphql.py — a small GraphQL executor for the documents the kit uses.
#
# Supports: anonymous and named query/mutation operations, variables,
# aliases, inline fragments, inline enum lists and input objects, null
# variables, and batching (a JSON array of operations -> an array of
# responses). Unknown fields are reported as HTTP 200 errors naming the
# field (the kit checks exactly that), never as a crash.
#
# Types implemented: Query, Mutation, User, Repository, Ref, Issue,
# IssueConnection/Edge, PullRequest, PullRequestConnection/Edge,
# PullRequestFileConnection, PullRequestFile, PageInfo, the mutation
# payloads, IssueComment, and __Type introspection (fields / enumValues).

import base64
import json

from store import CLOCK_EPOCH, global_id, iso_of, parse_global_id


class GraphQLError(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message


# ---------------------------------------------------------------------------
# lexer
# ---------------------------------------------------------------------------

_PUNCT = set("{}()[]:!=$@")
_NAME_START = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_")
_NAME_CHARS = _NAME_START | set("0123456789")


class Token:
    __slots__ = ("kind", "value")

    def __init__(self, kind, value):
        self.kind = kind
        self.value = value

    def __repr__(self):
        return "Token(%r, %r)" % (self.kind, self.value)


def _lex(text):
    tokens = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in " \t\r\n,":
            i += 1
            continue
        if ch == "#":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch == "." and text[i:i + 3] == "...":
            tokens.append(Token("...", "..."))
            i += 3
            continue
        if ch in _PUNCT:
            tokens.append(Token(ch, ch))
            i += 1
            continue
        if ch == '"':
            if text[i:i + 3] == '"""':
                end = text.find('"""', i + 3)
                if end < 0:
                    raise GraphQLError("unterminated block string")
                tokens.append(Token("string", text[i + 3:end]))
                i = end + 3
                continue
            j = i + 1
            out = []
            while j < n and text[j] != '"':
                if text[j] == "\\":
                    j += 1
                    if j >= n:
                        break
                    esc = text[j]
                    mapping = {"n": "\n", "t": "\t", "r": "\r", '"': '"',
                               "\\": "\\", "/": "/", "b": "\b", "f": "\f"}
                    if esc == "u" and j + 4 < n:
                        out.append(chr(int(text[j + 1:j + 5], 16)))
                        j += 5
                    else:
                        out.append(mapping.get(esc, esc))
                        j += 1
                else:
                    out.append(text[j])
                    j += 1
            if j >= n:
                raise GraphQLError("unterminated string")
            tokens.append(Token("string", "".join(out)))
            i = j + 1
            continue
        if ch.isdigit() or (ch == "-" and i + 1 < n and text[i + 1].isdigit()):
            j = i
            if text[j] == "-":
                j += 1
            while j < n and text[j].isdigit():
                j += 1
            is_float = False
            if j < n and text[j] == ".":
                is_float = True
                j += 1
                while j < n and text[j].isdigit():
                    j += 1
            if j < n and text[j] in "eE":
                is_float = True
                j += 1
                if j < n and text[j] in "+-":
                    j += 1
                while j < n and text[j].isdigit():
                    j += 1
            raw = text[i:j]
            tokens.append(Token("float" if is_float else "int",
                                float(raw) if is_float else int(raw)))
            i = j
            continue
        if ch in _NAME_START:
            j = i
            while j < n and text[j] in _NAME_CHARS:
                j += 1
            tokens.append(Token("name", text[i:j]))
            i = j
            continue
        raise GraphQLError("unexpected character %r" % ch)
    tokens.append(Token("eof", None))
    return tokens


class EnumValue:
    __slots__ = ("name",)

    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return "EnumValue(%r)" % self.name


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------

class Parser:
    def __init__(self, text):
        self.tokens = _lex(text)
        self.pos = 0

    def peek(self):
        return self.tokens[self.pos]

    def next(self):
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def expect(self, kind):
        tok = self.next()
        if tok.kind != kind:
            raise GraphQLError("expected %r, got %r" % (kind, tok.kind))
        return tok

    def expect_name(self, name=None):
        tok = self.next()
        if tok.kind != "name" or (name is not None and tok.value != name):
            raise GraphQLError("expected name %r, got %r" % (name, tok.value))
        return tok.value

    def parse_document(self):
        ops = []
        while self.peek().kind != "eof":
            ops.append(self.parse_definition())
        return ops

    def parse_definition(self):
        tok = self.peek()
        if tok.kind == "{":
            return {"type": "query", "name": None, "variables": {},
                    "selection": self.parse_selection_set()}
        if tok.kind == "name" and tok.value in ("query", "mutation", "subscription"):
            op_type = self.next().value
            name = None
            if self.peek().kind == "name":
                name = self.next().value
            variables = {}
            if self.peek().kind == "(":
                variables = self.parse_variable_definitions()
            return {"type": op_type, "name": name, "variables": variables,
                    "selection": self.parse_selection_set()}
        if tok.kind == "name" and tok.value == "fragment":
            # Named fragments are not used by the kit; parse and keep them so
            # a document that mentions one does not break the parser.
            self.next()
            frag_name = self.expect_name()
            self.expect_name("on")
            self.expect_name()
            frag = {"type": "fragment", "name": frag_name,
                    "selection": self.parse_selection_set()}
            return frag
        raise GraphQLError("unexpected token %r" % tok.value)

    def parse_variable_definitions(self):
        self.expect("(")
        variables = {}
        while self.peek().kind != ")":
            self.expect("$")
            name = self.expect_name()
            self.expect(":")
            self.parse_type()
            default = None
            if self.peek().kind == "=":
                self.next()
                default = self.parse_value()
            variables[name] = default
        self.expect(")")
        return variables

    def parse_type(self):
        tok = self.peek()
        if tok.kind == "[":
            self.next()
            self.parse_type()
            self.expect("]")
        elif tok.kind == "name":
            self.next()
        else:
            raise GraphQLError("expected a type, got %r" % tok.value)
        if self.peek().kind == "!":
            self.next()

    def parse_selection_set(self):
        self.expect("{")
        selections = []
        while self.peek().kind != "}":
            selections.append(self.parse_selection())
        self.expect("}")
        return selections

    def parse_selection(self):
        if self.peek().kind == "...":
            self.next()
            if self.peek().kind == "name" and self.peek().value == "on":
                self.next()
                type_name = self.expect_name()
                return {"kind": "inline", "type": type_name,
                        "selection": self.parse_selection_set()}
            frag_name = self.expect_name()
            return {"kind": "spread", "name": frag_name}
        name = self.expect_name()
        alias = None
        if self.peek().kind == ":":
            self.next()
            alias = name
            name = self.expect_name()
        args = {}
        if self.peek().kind == "(":
            args = self.parse_arguments()
        self.parse_directives()
        selection = None
        if self.peek().kind == "{":
            selection = self.parse_selection_set()
        return {"kind": "field", "name": name, "alias": alias or name,
                "args": args, "selection": selection}

    def parse_arguments(self):
        self.expect("(")
        args = {}
        while self.peek().kind != ")":
            name = self.expect_name()
            self.expect(":")
            args[name] = self.parse_value()
        self.expect(")")
        return args

    def parse_directives(self):
        while self.peek().kind == "@":
            self.next()
            self.expect_name()
            if self.peek().kind == "(":
                self.parse_arguments()

    def parse_value(self):
        tok = self.next()
        if tok.kind == "$":
            return ("var", self.expect_name())
        if tok.kind == "string":
            return tok.value
        if tok.kind in ("int", "float"):
            return tok.value
        if tok.kind == "[":
            values = []
            while self.peek().kind != "]":
                values.append(self.parse_value())
            self.expect("]")
            return values
        if tok.kind == "{":
            obj = {}
            while self.peek().kind != "}":
                key = self.expect_name()
                self.expect(":")
                obj[key] = self.parse_value()
            self.expect("}")
            return obj
        if tok.kind == "name":
            if tok.value == "true":
                return True
            if tok.value == "false":
                return False
            if tok.value == "null":
                return None
            return EnumValue(tok.value)
        raise GraphQLError("unexpected value token %r" % tok.value)


def _resolve_value(value, variables):
    """Substitute variables and unwrap enums to plain strings."""
    if isinstance(value, tuple) and len(value) == 2 and value[0] == "var":
        return variables.get(value[1])
    if isinstance(value, EnumValue):
        return value.name
    if isinstance(value, list):
        return [_resolve_value(v, variables) for v in value]
    if isinstance(value, dict):
        return {k: _resolve_value(v, variables) for k, v in value.items()}
    return value


# ---------------------------------------------------------------------------
# schema (for __type introspection)
# ---------------------------------------------------------------------------

_SCHEMA = {
    "Issue": {"kind": "OBJECT", "fields": ["id", "number", "title", "state",
                                           "body", "createdAt", "updatedAt",
                                           "labels", "comments"]},
    "PullRequest": {"kind": "OBJECT", "fields": ["id", "number", "title",
                                                 "state", "body", "headRefName",
                                                 "baseRefName", "mergeable",
                                                 "merged", "mergedAt", "files",
                                                 "headRef", "baseRef"]},
    "Repository": {"kind": "OBJECT", "fields": ["id", "databaseId", "name",
                                                "nameWithOwner", "owner",
                                                "defaultBranchRef",
                                                "viewerPermission",
                                                "hasIssuesEnabled", "issue",
                                                "issues", "pullRequests",
                                                "pullRequest", "labels",
                                                "description", "private"]},
    "SearchType": {"kind": "ENUM", "enumValues": ["ISSUE", "REPOSITORY",
                                                  "USER", "CODE", "DISCUSSION"]},
    "User": {"kind": "OBJECT", "fields": ["id", "login"]},
    "Ref": {"kind": "OBJECT", "fields": ["id", "name"]},
}


def _cursor(index):
    return base64.b64encode(("cursor:%d" % index).encode()).decode()


def _uncursor(cursor):
    try:
        raw = base64.b64decode(cursor.encode()).decode()
        if raw.startswith("cursor:"):
            return int(raw[len("cursor:"):])
    except Exception:
        pass
    return None


def _paginate_nodes(items, args):
    """Apply first/after to an already-ordered list.
    Returns (page, pageInfo, start_index)."""
    start = 0
    after = args.get("after")
    if after is not None:
        idx = _uncursor(after)
        if idx is not None:
            start = idx + 1
    first = args.get("first")
    if first is None:
        page = items[start:]
    else:
        page = items[start:start + int(first)]
    if page:
        end_cursor = _cursor(start + len(page) - 1)
    else:
        end_cursor = None
    has_next = (start + len(page)) < len(items)
    page_info = {
        "__typename": "PageInfo",
        "hasNextPage": has_next,
        "endCursor": end_cursor,
        "hasPreviousPage": start > 0,
        "startCursor": _cursor(start) if page else None,
    }
    return page, page_info, start


def _connection(items, args, node_typename):
    page, page_info, start = _paginate_nodes(items, args)
    edges = [{"__typename": node_typename + "Edge", "node": item,
              "cursor": _cursor(start + i)} for i, item in enumerate(page)]
    return {"__typename": node_typename + "Connection", "nodes": page,
            "edges": edges, "pageInfo": page_info, "totalCount": len(items)}


# ---------------------------------------------------------------------------
# executor
# ---------------------------------------------------------------------------

class Executor:
    def __init__(self, store, variables):
        self.store = store
        self.variables = variables or {}
        self.errors = []

    # -- value builders -------------------------------------------------
    def _repo_value(self, repo):
        return {"__typename": "Repository", "__repo": repo}

    def _issue_value(self, repo, issue):
        return {"__typename": "Issue", "__repo": repo, "__issue": issue}

    def _pr_value(self, repo, pr):
        return {"__typename": "PullRequest", "__repo": repo, "__pr": pr}

    def _file_value(self, filename):
        return {"__typename": "PullRequestFile", "__path": filename}

    # -- field resolution ------------------------------------------------
    def resolve_field(self, type_name, value, field, args):
        name = field["name"]
        if type_name == "Query":
            return self._query_field(name, args)
        if type_name == "Mutation":
            return self._mutation_field(name, args)
        if type_name == "User":
            if name == "login":
                return value.get("__login", "")
            if name == "id":
                return global_id("User", value.get("__login", ""))
        if type_name == "Repository":
            return self._repository_field(value, name, args)
        if type_name == "Ref":
            if name == "name":
                return value.get("__ref_name", "")
            if name == "id":
                return global_id("Ref", value.get("__ref_name", ""))
        if type_name == "Issue":
            return self._issue_field(value, name, args)
        if type_name == "PullRequest":
            return self._pr_field(value, name, args)
        if type_name == "IssueConnection":
            if name == "nodes":
                return value["nodes"]
            if name == "edges":
                return value["edges"]
            if name == "pageInfo":
                return value["pageInfo"]
            if name == "totalCount":
                return value["totalCount"]
        if type_name == "IssueEdge":
            if name == "node":
                return value["node"]
            if name == "cursor":
                return value["cursor"]
        if type_name == "PullRequestConnection":
            if name == "nodes":
                return value["nodes"]
            if name == "edges":
                return value["edges"]
            if name == "pageInfo":
                return value["pageInfo"]
            if name == "totalCount":
                return value["totalCount"]
        if type_name == "PullRequestEdge":
            if name == "node":
                return value["node"]
            if name == "cursor":
                return value["cursor"]
        if type_name == "PullRequestFileConnection":
            if name == "nodes":
                return value["nodes"]
            if name == "edges":
                return value["edges"]
            if name == "pageInfo":
                return value["pageInfo"]
        if type_name == "PullRequestFileEdge":
            if name == "node":
                return value["node"]
            if name == "cursor":
                return value["cursor"]
        if type_name == "PullRequestFile":
            if name == "path":
                return value["__path"]
        if type_name == "PageInfo":
            if name in ("hasNextPage", "endCursor", "hasPreviousPage", "startCursor"):
                return value[name]
        if type_name == "__Type":
            return self._type_field(value, name, args)
        if type_name == "__Field":
            if name == "name":
                return value["name"]
        if type_name == "__EnumValue":
            if name == "name":
                return value["name"]
        if type_name in ("CreateIssuePayload", "CloseIssuePayload"):
            if name == "issue":
                return value.get("issue")
        if type_name == "CreatePullRequestPayload":
            if name == "pullRequest":
                return value.get("pullRequest")
        if type_name == "MergePullRequestPayload":
            if name == "pullRequest":
                return value.get("pullRequest")
        if type_name == "AddCommentPayload":
            if name == "commentEdge":
                return value.get("commentEdge")
        if type_name == "IssueCommentEdge":
            if name == "node":
                return value.get("node")
            if name == "cursor":
                return value.get("cursor")
        if type_name == "IssueComment":
            if name == "id":
                return value.get("__id")
            if name == "body":
                return value.get("__body")
        self.errors.append({"message": "Cannot query field \"%s\" on type \"%s\"" % (name, type_name)})
        return None

    def _query_field(self, name, args):
        store = self.store
        if name == "viewer":
            return {"__typename": "User", "__login": store.viewer_login or ""}
        if name == "repository":
            owner = args.get("owner")
            repo_name = args.get("name")
            if store.viewer_login is not None and owner != store.viewer_login:
                return None
            repo = store.repos.get(repo_name)
            if repo is None:
                return None
            return self._repo_value(repo)
        if name == "node":
            gid = args.get("id")
            decoded = parse_global_id(gid) if gid else None
            if decoded is None:
                return None
            kind, key = decoded
            if kind == "Repository":
                repo = store.repos.get(key)
                return self._repo_value(repo) if repo else None
            if kind in ("Issue", "PullRequest"):
                parts = key.split(":")
                if len(parts) != 2:
                    return None
                repo = store.repos.get(parts[0])
                if repo is None:
                    return None
                try:
                    number = int(parts[1])
                except ValueError:
                    return None
                if kind == "Issue":
                    issue = repo.issue_by_number(number)
                    return self._issue_value(repo, issue) if issue else None
                pr = repo.pull_by_number(number)
                return self._pr_value(repo, pr) if pr else None
            return None
        if name == "__type":
            type_name = args.get("name")
            info = _SCHEMA.get(type_name)
            if info is None:
                return None
            return {"__typename": "__Type", "__type_name": type_name, "__info": info}
        if name == "__schema":
            return {"__typename": "__Schema"}
        self.errors.append({"message": "Cannot query field \"%s\" on type \"Query\"" % name})
        return None

    def _repository_field(self, value, name, args):
        repo = value["__repo"]
        store = self.store
        if name == "id":
            return global_id("Repository", repo.name)
        if name == "databaseId":
            return repo.database_id
        if name == "name":
            return repo.name
        if name == "nameWithOwner":
            return "%s/%s" % (store.viewer_login or "", repo.name)
        if name == "owner":
            return {"__typename": "User", "__login": store.viewer_login or ""}
        if name == "defaultBranchRef":
            tip = repo.branch_tip(repo.default_branch)
            if tip is None:
                return None
            return {"__typename": "Ref", "__ref_name": repo.default_branch}
        if name == "viewerPermission":
            return "ADMIN"
        if name == "hasIssuesEnabled":
            return True
        if name == "description":
            return repo.description
        if name == "private":
            return repo.private
        if name == "issue":
            number = args.get("number")
            if number is None:
                return None
            issue = repo.issue_by_number(int(number))
            return self._issue_value(repo, issue) if issue else None
        if name == "issues":
            issues = [repo.issues[k] for k in sorted(repo.issues)]
            states = args.get("states")
            if states:
                wanted = {str(s).lower() for s in states}
                issues = [i for i in issues if i.state in wanted]
            order_by = args.get("orderBy") or {}
            direction = str(order_by.get("direction", "DESC")).upper()
            issues = list(reversed(issues)) if direction == "DESC" else issues
            values = [self._issue_value(repo, i) for i in issues]
            return _connection(values, args, "Issue")
        if name == "pullRequests":
            pulls = [repo.pulls[k] for k in sorted(repo.pulls)]
            states = args.get("states")
            if states:
                wanted = {str(s).lower() for s in states}
                pulls = [p for p in pulls if p.state in wanted]
            values = [self._pr_value(repo, p) for p in pulls]
            return _connection(values, args, "PullRequest")
        if name == "pullRequest":
            number = args.get("number")
            if number is None:
                return None
            pr = repo.pull_by_number(int(number))
            return self._pr_value(repo, pr) if pr else None
        self.errors.append({"message": "Cannot query field \"%s\" on type \"Repository\"" % name})
        return None

    def _issue_field(self, value, name, args):
        issue = value["__issue"]
        repo = value["__repo"]
        if name == "id":
            return global_id("Issue", "%s:%d" % (repo.name, issue.number))
        if name == "number":
            return issue.number
        if name == "title":
            return issue.title
        if name == "state":
            return issue.state.upper()
        if name == "body":
            return issue.body
        if name == "createdAt":
            return iso_of(CLOCK_EPOCH + issue.seq)
        if name == "updatedAt":
            return iso_of(CLOCK_EPOCH + issue.seq)
        self.errors.append({"message": "Cannot query field \"%s\" on type \"Issue\"" % name})
        return None

    def _pr_field(self, value, name, args):
        pr = value["__pr"]
        repo = value["__repo"]
        if name == "id":
            return global_id("PullRequest", "%s:%d" % (repo.name, pr.number))
        if name == "number":
            return pr.number
        if name == "title":
            return pr.title
        if name == "state":
            return pr.state.upper()
        if name == "body":
            return pr.body
        if name == "headRefName":
            return pr.head
        if name == "baseRefName":
            return pr.base
        if name == "mergeable":
            return self.store.pr_mergeable(repo, pr)
        if name == "merged":
            return pr.merged
        if name == "mergedAt":
            return pr.merged_at
        if name == "files":
            files = self.store.pr_files(repo, pr)
            values = [self._file_value(f["filename"]) for f in files]
            return _connection(values, args, "PullRequestFile")
        self.errors.append({"message": "Cannot query field \"%s\" on type \"PullRequest\"" % name})
        return None

    def _type_field(self, value, name, args):
        info = value["__info"]
        if name == "name":
            return value["__type_name"]
        if name == "kind":
            return info["kind"]
        if name == "fields":
            return [{"__typename": "__Field", "name": f} for f in info.get("fields", [])]
        if name == "enumValues":
            return [{"__typename": "__EnumValue", "name": v} for v in info.get("enumValues", [])]
        if name == "description":
            return None
        self.errors.append({"message": "Cannot query field \"%s\" on type \"__Type\"" % name})
        return None

    def _mutation_field(self, name, args):
        store = self.store
        inp = args.get("input") or {}
        if name == "createIssue":
            repo_id = inp.get("repositoryId")
            decoded = parse_global_id(repo_id) if repo_id else None
            repo = store.repos.get(decoded[1]) if decoded else None
            if repo is None:
                self.errors.append({"message": "repository not found"})
                return None
            status, result = store.create_issue(repo, inp.get("title") or "",
                                                inp.get("body") or "")
            if status != 201:
                self.errors.append({"message": result.get("message", "Validation Failed")})
                return None
            return {"__typename": "CreateIssuePayload",
                    "issue": self._issue_value(repo, result)}
        if name == "closeIssue":
            decoded = parse_global_id(inp.get("issueId")) if inp.get("issueId") else None
            if decoded is None:
                self.errors.append({"message": "issue not found"})
                return None
            parts = decoded[1].split(":")
            repo = store.repos.get(parts[0]) if len(parts) == 2 else None
            issue = repo.issue_by_number(int(parts[1])) if repo else None
            if issue is None:
                self.errors.append({"message": "issue not found"})
                return None
            store.set_issue_state(repo, issue.number, "closed")
            return {"__typename": "CloseIssuePayload",
                    "issue": self._issue_value(repo, issue)}
        if name == "createPullRequest":
            decoded = parse_global_id(inp.get("repositoryId")) if inp.get("repositoryId") else None
            repo = store.repos.get(decoded[1]) if decoded else None
            if repo is None:
                self.errors.append({"message": "repository not found"})
                return None
            status, result = store.create_pull(
                repo, inp.get("headRefName") or "", inp.get("baseRefName") or "",
                inp.get("title") or "", inp.get("body") or "")
            if status != 201:
                self.errors.append({"message": result.get("message", "Validation Failed")})
                return None
            return {"__typename": "CreatePullRequestPayload",
                    "pullRequest": self._pr_value(repo, result)}
        if name == "mergePullRequest":
            decoded = parse_global_id(inp.get("pullRequestId")) if inp.get("pullRequestId") else None
            if decoded is None:
                self.errors.append({"message": "pull request not found"})
                return None
            parts = decoded[1].split(":")
            repo = store.repos.get(parts[0]) if len(parts) == 2 else None
            pr = repo.pull_by_number(int(parts[1])) if repo else None
            if pr is None:
                self.errors.append({"message": "pull request not found"})
                return None
            method = str(inp.get("mergeMethod") or "SQUASH").lower()
            status, result = store.merge_pull(repo, pr, method)
            if status != 200:
                self.errors.append({"message": result.get("message", "error")})
                return None
            return {"__typename": "MergePullRequestPayload",
                    "pullRequest": self._pr_value(repo, pr)}
        if name == "addComment":
            decoded = parse_global_id(inp.get("subjectId")) if inp.get("subjectId") else None
            if decoded is None:
                self.errors.append({"message": "subject not found"})
                return None
            parts = decoded[1].split(":")
            repo = store.repos.get(parts[0]) if len(parts) >= 2 else None
            issue = repo.issue_by_number(int(parts[1])) if repo else None
            if issue is None:
                self.errors.append({"message": "subject not found"})
                return None
            comment = store.add_comment(repo, issue, inp.get("body") or "")
            node = {"__typename": "IssueComment",
                    "__id": global_id(
                        "IssueComment", "%s:%d:%d" % (repo.name, issue.number, comment.cid)),
                    "__body": comment.body}
            return {"__typename": "AddCommentPayload",
                    "commentEdge": {"__typename": "IssueCommentEdge",
                                    "node": node, "cursor": _cursor(0)}}
        self.errors.append({"message": "Cannot query field \"%s\" on type \"Mutation\"" % name})
        return None

    # -- selection execution ----------------------------------------------
    def execute_selection(self, type_name, value, selection_set):
        out = {}
        for sel in selection_set:
            if sel["kind"] == "inline":
                if sel["type"] is None or (isinstance(value, dict) and
                                           value.get("__typename") == sel["type"]):
                    out.update(self.execute_selection(type_name, value, sel["selection"]))
                continue
            if sel["kind"] == "spread":
                continue  # named fragments are not used by the kit
            args = {k: _resolve_value(v, self.variables) for k, v in sel["args"].items()}
            resolved = self.resolve_field(type_name, value, sel, args)
            if sel["selection"] is None:
                out[sel["alias"]] = resolved
                continue
            if resolved is None:
                out[sel["alias"]] = None
                continue
            if isinstance(resolved, list):
                out[sel["alias"]] = [
                    self._execute_value(item, sel["selection"]) for item in resolved
                ]
            else:
                out[sel["alias"]] = self._execute_value(resolved, sel["selection"])
        return out

    def _execute_value(self, value, selection_set):
        if not isinstance(value, dict):
            return value
        type_name = value.get("__typename")
        if type_name is None:
            return value
        return self.execute_selection(type_name, value, selection_set)


def execute_operation(store, query_text, variables):
    """Execute one GraphQL operation. Returns a response dict."""
    try:
        ops = Parser(query_text).parse_document()
    except GraphQLError as err:
        return {"errors": [{"message": err.message}]}
    op = None
    for candidate in ops:
        if candidate.get("type") in ("query", "mutation"):
            op = candidate
            break
    if op is None:
        return {"errors": [{"message": "no operation found"}]}
    executor = Executor(store, variables)
    root_type = "Query" if op["type"] == "query" else "Mutation"
    data = executor.execute_selection(root_type, {}, op["selection"])
    response = {"data": data}
    if executor.errors:
        response["errors"] = executor.errors
    return response


def handle_graphql(store, body):
    """body: raw request bytes. Returns (status, json_object)."""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return 400, {"errors": [{"message": "request body is not valid JSON"}]}
    if isinstance(payload, list):
        return 200, [execute_operation(store, item.get("query", ""),
                                       item.get("variables") or {})
                     for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return 400, {"errors": [{"message": "request body must be an object"}]}
    return 200, execute_operation(store, payload.get("query", ""),
                                  payload.get("variables") or {})
