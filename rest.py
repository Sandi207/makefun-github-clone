# rest.py — REST v3 API under /api/v3.
#
# Protocol discipline (from the kit's checks):
#   * every reply is application/json, never HTML;
#   * every reply carries a non-empty X-GitHub-Request-Id (added by server.py);
#   * error bodies are {"message": string};
#   * unknown route -> JSON 404, malformed JSON body -> 400;
#   * no token -> 401 (enforced by server.py), wrong owner -> 404, never 403.
#
# The handler is a pure function: dispatch(store, method, path, query, body,
# base_url) -> (status, extra_headers, json_object). The server serializes
# the object and adds the request-id header.

import base64
import json
import urllib.parse

from store import CLOCK_EPOCH, ConflictError, ExistsError, NotFoundError, iso_of


def _ts(seq):
    return iso_of(CLOCK_EPOCH + seq)


class RestError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def _owner(store):
    return store.viewer_login or ""


def _repo_json(store, repo):
    owner = _owner(store)
    return {
        "id": repo.database_id,
        "name": repo.name,
        "full_name": "%s/%s" % (owner, repo.name) if owner else repo.name,
        "owner": {"login": owner},
        "private": repo.private,
        "description": repo.description,
        "default_branch": repo.default_branch,
        "created_at": _ts(repo.seq),
        "updated_at": _ts(repo.seq),
    }


def _label_json(label):
    return {"name": label["name"], "color": label["color"]}


def _issue_json(store, repo, issue):
    labels = []
    for name in issue.labels:
        label = repo.labels.get(name)
        labels.append({"name": name, "color": label["color"] if label else ""})
    return {
        "id": issue.gid,
        "number": issue.number,
        "title": issue.title,
        "body": issue.body,
        "state": issue.state,
        "labels": labels,
        "created_at": _ts(issue.seq),
        "updated_at": _ts(issue.seq),
    }


def _pull_json(repo, pr):
    head_sha = repo.branch_tip(pr.head) or ""
    base_sha = repo.branch_tip(pr.base) or ""
    return {
        "id": pr.gid,
        "number": pr.number,
        "title": pr.title,
        "body": pr.body,
        "state": pr.state,
        "head": {"ref": pr.head, "sha": pr.head_sha or head_sha, "label": pr.head},
        "base": {"ref": pr.base, "sha": pr.base_sha or base_sha, "label": pr.base},
        "merged": pr.merged,
        "merged_at": pr.merged_at,
        "merge_commit_sha": pr.merge_commit_sha,
        "created_at": _ts(pr.seq),
        "updated_at": _ts(pr.seq),
    }


def _comment_json(comment):
    return {"id": comment.cid, "body": comment.body,
            "created_at": _ts(comment.seq)}


def _get_repo_or_404(store, owner, name):
    """Wrong owner -> 404 (never 403); unknown repo -> 404."""
    if store.viewer_login is not None and owner != store.viewer_login:
        raise RestError(404, "Not Found")
    repo = store.repos.get(name)
    if repo is None:
        raise RestError(404, "Not Found")
    return repo


def _parse_json(body):
    if body is None or body == b"":
        return {}
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise RestError(400, "Problems parsing JSON")
    if not isinstance(data, dict):
        raise RestError(400, "Problems parsing JSON")
    return data


def _int_query(query, key, default):
    try:
        return int(query.get(key, [str(default)])[0])
    except (ValueError, TypeError):
        return default


def _page_link(base_url, path, query, page, per_page):
    params = dict(query)
    params["page"] = [str(page)]
    params["per_page"] = [str(per_page)]
    qs = urllib.parse.urlencode({k: v[0] for k, v in params.items()})
    return "<%s%s?%s>; rel=\"%s\"" % (base_url, path, qs, "next")


def _paginate(items, query, base_url, path, default_per_page=30):
    """Return (page_items, link_header_or_None)."""
    per_page = max(1, _int_query(query, "per_page", default_per_page))
    page = max(1, _int_query(query, "page", 1))
    start = (page - 1) * per_page
    end = start + per_page
    page_items = items[start:end]
    links = []
    if end < len(items):
        links.append(_page_link(base_url, path, query, page + 1, per_page))
        last = (len(items) + per_page - 1) // per_page
        if last > page + 1:
            params = dict(query)
            params["page"] = [str(last)]
            params["per_page"] = [str(per_page)]
            qs = urllib.parse.urlencode({k: v[0] for k, v in params.items()})
            links.append("<%s%s?%s>; rel=\"last\"" % (base_url, path, qs))
    return page_items, (", ".join(links) if links else None)


def _resolve_commit(repo, ref):
    """A ref name (branch) or a bare oid -> commit oid, or None."""
    if ref is None:
        return repo.branch_tip(repo.default_branch)
    tip = repo.branch_tip(ref)
    if tip is not None:
        return tip
    if len(ref) == 40 and all(c in "0123456789abcdef" for c in ref):
        obj = repo.get_object(ref)
        if obj is not None and obj[0] == "commit":
            return ref
    return None


def _tree_at(repo, ref):
    """(tree_oid, commit_oid) for a ref, or (None, None)."""
    commit_oid = _resolve_commit(repo, ref)
    if commit_oid is None:
        return None, None
    import gitobj
    obj = repo.get_object(commit_oid)
    if obj is None or obj[0] != "commit":
        return None, None
    return gitobj.parse_commit(obj[1])["tree"], commit_oid


def _lookup_path(repo, ref, path):
    """Walk the tree at ref to path -> ("file", blob_oid) | ("dir", tree_oid)
    | ("missing", None)."""
    import gitobj
    tree_oid, _ = _tree_at(repo, ref)
    if tree_oid is None:
        return "missing", None
    if path in ("", "/"):
        return "dir", tree_oid
    parts = [p for p in path.split("/") if p != ""]
    cur = tree_oid
    for i, part in enumerate(parts):
        obj = repo.get_object(cur)
        if obj is None or obj[0] != "tree":
            return "missing", None
        entries = gitobj.parse_tree(obj[1])
        hit = None
        for mode, name, oid in entries:
            if name == part:
                hit = (mode, oid)
                break
        if hit is None:
            return "missing", None
        mode, oid = hit
        if i == len(parts) - 1:
            if mode == "40000":
                return "dir", oid
            return "file", oid
        if mode != "40000":
            return "missing", None
        cur = oid
    return "missing", None


def _dir_listing(repo, tree_oid, path):
    import gitobj
    obj = repo.get_object(tree_oid)
    if obj is None or obj[0] != "tree":
        return []
    out = []
    for mode, name, oid in gitobj.parse_tree(obj[1]):
        child_path = ("%s/%s" % (path, name)) if path else name
        out.append({
            "type": "dir" if mode == "40000" else "file",
            "name": name,
            "path": child_path,
            "sha": oid,
        })
    return out


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------

def dispatch(store, method, path, query, body, base_url):
    """path is the URL path after /api/v3 (e.g. "/repos/x/y/issues").
    Returns (status, extra_headers_dict, json_object_or_None)."""
    segs = [s for s in path.split("/") if s != ""]

    if method == "GET" and segs == []:
        return 200, {"X-OAuth-Scopes": "repo, read:user"}, {"current_user_url": base_url + "/api/v3/user"}

    if method == "GET" and segs == ["meta"]:
        return 200, {}, {"installed_version": "1.0.0"}

    if method == "GET" and segs == ["user"]:
        if store.viewer_login is None:
            raise RestError(404, "Not Found")
        return 200, {}, {"login": store.viewer_login}

    if method == "POST" and segs == ["user", "repos"]:
        data = _parse_json(body)
        name = data.get("name")
        if not name or not isinstance(name, str):
            raise RestError(422, "Validation Failed")
        if name in store.repos:
            raise RestError(422, "name already exists on this account")
        repo = store.create_repo(name, private=bool(data.get("private", False)),
                                 description=data.get("description"))
        return 201, {}, _repo_json(store, repo)

    if method == "GET" and segs == ["user", "repos"]:
        repos = store.list_repos()
        items, link = _paginate(repos, query, base_url, "/api/v3/user/repos")
        headers = {}
        if link:
            headers["Link"] = link
        return 200, headers, [_repo_json(store, r) for r in items]

    # /repos/{owner}/{repo}[...]
    if len(segs) >= 3 and segs[0] == "repos":
        owner, name = segs[1], segs[2]
        rest = segs[3:]

        if method == "GET" and rest == []:
            repo = _get_repo_or_404(store, owner, name)
            return 200, {}, _repo_json(store, repo)

        if method == "PATCH" and rest == []:
            repo = _get_repo_or_404(store, owner, name)
            data = _parse_json(body)
            if "description" in data:
                repo.description = data["description"]
            if "default_branch" in data and isinstance(data["default_branch"], str):
                repo.default_branch = data["default_branch"]
            return 200, {}, _repo_json(store, repo)

        if method == "DELETE" and rest == []:
            repo = _get_repo_or_404(store, owner, name)
            store.delete_repo(repo.name)
            return 204, {}, None

        if method == "POST" and rest == ["git", "refs"]:
            repo = _get_repo_or_404(store, owner, name)
            data = _parse_json(body)
            ref = data.get("ref")
            sha = data.get("sha")
            if not ref or not sha:
                raise RestError(422, "Validation Failed")
            if not ref.startswith("refs/heads/"):
                raise RestError(422, "Validation Failed")
            repo.set_ref(ref, sha)
            return 201, {}, {"ref": ref, "object": {"sha": sha}}

        if method == "GET" and len(rest) >= 3 and rest[0] == "git" and rest[1] == "refs":
            repo = _get_repo_or_404(store, owner, name)
            ref = "refs/" + "/".join(rest[2:])
            oid = repo.get_ref(ref)
            if oid is None:
                raise RestError(404, "Not Found")
            return 200, {}, {"ref": ref, "object": {"sha": oid}}

        if method == "GET" and rest == ["branches"]:
            repo = _get_repo_or_404(store, owner, name)
            out = []
            for ref in sorted(repo.refs):
                if ref.startswith("refs/heads/"):
                    out.append({"name": ref[len("refs/heads/"):],
                                "commit": {"sha": repo.refs[ref]}})
            return 200, {}, out

        if method == "GET" and len(rest) >= 2 and rest[0] == "branches":
            repo = _get_repo_or_404(store, owner, name)
            branch = "/".join(rest[1:])
            tip = repo.branch_tip(branch)
            if tip is None:
                raise RestError(404, "Not Found")
            return 200, {}, {"name": branch, "commit": {"sha": tip}}

        if method == "GET" and rest == ["commits"]:
            repo = _get_repo_or_404(store, owner, name)
            ref = query.get("sha", [None])[0]
            tip = _resolve_commit(repo, ref)
            if tip is None:
                return 200, {}, []
            import gitobj
            commits = store.reachable_commits(repo, [tip])
            out = []
            for sha in commits:
                obj = repo.get_object(sha)
                if obj is None or obj[0] != "commit":
                    continue
                out.append({"sha": sha,
                            "commit": {"message": gitobj.parse_commit(obj[1])["message"]}})
            return 200, {}, out

        if len(rest) >= 1 and rest[0] == "contents":
            cpath = "/".join(rest[1:])
            if method == "PUT":
                repo = _get_repo_or_404(store, owner, name)
                data = _parse_json(body)
                message = data.get("message", "")
                content_b64 = data.get("content")
                if content_b64 is None:
                    raise RestError(422, "Validation Failed")
                try:
                    raw = base64.b64decode(content_b64, validate=False)
                except Exception:
                    raise RestError(422, "Validation Failed")
                branch = data.get("branch") or repo.default_branch
                sha = data.get("sha")
                try:
                    commit_oid, blob_oid, status = store.commit_file(
                        repo, branch, cpath, raw, message, expected_sha=sha)
                except ConflictError as err:
                    raise RestError(409, str(err))
                except ExistsError as err:
                    raise RestError(422, str(err))
                except NotFoundError as err:
                    raise RestError(404, str(err))
                return status, {}, {"content": {"sha": blob_oid, "encoding": "base64"},
                                    "commit": {"sha": commit_oid}}
            if method == "DELETE":
                repo = _get_repo_or_404(store, owner, name)
                data = _parse_json(body)
                message = data.get("message", "")
                sha = data.get("sha")
                branch = data.get("branch") or repo.default_branch
                try:
                    commit_oid = store.delete_file(repo, branch, cpath, message,
                                                   expected_sha=sha)
                except ConflictError as err:
                    raise RestError(409, str(err))
                except NotFoundError as err:
                    raise RestError(404, str(err))
                return 200, {}, {"commit": {"sha": commit_oid}}
            if method == "GET":
                repo = _get_repo_or_404(store, owner, name)
                ref = query.get("ref", [None])[0]
                kind, oid = _lookup_path(repo, ref, cpath)
                if kind == "missing":
                    raise RestError(404, "Not Found")
                if kind == "dir":
                    return 200, {}, _dir_listing(repo, oid, cpath)
                obj = repo.get_object(oid)
                if obj is None or obj[0] != "blob":
                    raise RestError(404, "Not Found")
                data = obj[1]
                return 200, {}, {
                    "type": "file",
                    "encoding": "base64",
                    "content": base64.b64encode(data).decode("ascii"),
                    "sha": oid,
                    "size": len(data),
                    "name": cpath.rsplit("/", 1)[-1],
                    "path": cpath,
                }

        if method == "POST" and rest == ["labels"]:
            repo = _get_repo_or_404(store, owner, name)
            data = _parse_json(body)
            lname = data.get("name")
            color = data.get("color", "")
            if not lname:
                raise RestError(422, "Validation Failed")
            label = repo.add_label(lname, color)
            return 201, {}, _label_json(label)

        if method == "GET" and rest == ["labels"]:
            repo = _get_repo_or_404(store, owner, name)
            return 200, {}, [_label_json(repo.labels[k]) for k in sorted(repo.labels)]

        if rest == ["issues"] and method in ("GET", "POST"):
            repo = _get_repo_or_404(store, owner, name)
            if method == "POST":
                data = _parse_json(body)
                title = data.get("title")
                if not title:
                    raise RestError(422, "Validation Failed")
                status, result = store.create_issue(
                    repo, title, data.get("body") or "",
                    labels=data.get("labels") or [])
                if status == 422:
                    raise RestError(422, result.get("message", "Validation Failed"))
                return 201, {}, _issue_json(store, repo, result)
            state = query.get("state", ["open"])[0]
            direction = query.get("direction", ["desc"])[0]
            issues = [repo.issues[k] for k in sorted(repo.issues)]
            if state == "open":
                issues = [i for i in issues if i.state == "open"]
            elif state == "closed":
                issues = [i for i in issues if i.state == "closed"]
            if direction == "desc":
                issues = list(reversed(issues))
            items, _link = _paginate(issues, query, base_url, "/api/v3" + path)
            return 200, {}, [_issue_json(store, repo, i) for i in items]

        if len(rest) >= 2 and rest[0] == "issues" and rest[1].isdigit():
            number = int(rest[1])
            repo = _get_repo_or_404(store, owner, name)
            issue = repo.issue_by_number(number)
            if issue is None:
                raise RestError(404, "Not Found")
            if method == "GET" and len(rest) == 2:
                return 200, {}, _issue_json(store, repo, issue)
            if method == "PATCH" and len(rest) == 2:
                data = _parse_json(body)
                state = data.get("state")
                if state in ("open", "closed"):
                    store.set_issue_state(repo, number, state)
                return 200, {}, _issue_json(store, repo, issue)
            if method == "GET" and len(rest) == 3 and rest[2] == "comments":
                return 200, {}, [_comment_json(c) for c in issue.comments]
            if method == "POST" and len(rest) == 3 and rest[2] == "comments":
                data = _parse_json(body)
                body_text = data.get("body")
                if not body_text:
                    raise RestError(422, "Validation Failed")
                comment = store.add_comment(repo, issue, body_text)
                return 201, {}, _comment_json(comment)

        if rest == ["pulls"] and method in ("GET", "POST"):
            repo = _get_repo_or_404(store, owner, name)
            if method == "POST":
                data = _parse_json(body)
                head = data.get("head")
                base = data.get("base")
                title = data.get("title")
                if not head or not base or not title:
                    raise RestError(422, "Validation Failed")
                status, result = store.create_pull(
                    repo, head, base, title, data.get("body") or "")
                if status == 422:
                    raise RestError(422, result.get("message", "Validation Failed"))
                return 201, {}, _pull_json(repo, result)
            state = query.get("state", ["open"])[0]
            pulls = [repo.pulls[k] for k in sorted(repo.pulls)]
            if state == "open":
                pulls = [p for p in pulls if p.state == "open"]
            elif state == "closed":
                pulls = [p for p in pulls if p.state == "closed"]
            items, _link = _paginate(pulls, query, base_url, "/api/v3" + path)
            return 200, {}, [_pull_json(repo, p) for p in items]

        if len(rest) >= 2 and rest[0] == "pulls" and rest[1].isdigit():
            number = int(rest[1])
            repo = _get_repo_or_404(store, owner, name)
            pr = repo.pull_by_number(number)
            if pr is None:
                raise RestError(404, "Not Found")
            if method == "GET" and len(rest) == 2:
                return 200, {}, _pull_json(repo, pr)
            if method == "GET" and len(rest) == 3 and rest[2] == "files":
                return 200, {}, store.pr_files(repo, pr)
            if method == "PUT" and len(rest) == 3 and rest[2] == "merge":
                data = _parse_json(body)
                method_name = data.get("merge_method", "squash")
                status, result = store.merge_pull(repo, pr, method_name)
                if status in (404, 405):
                    raise RestError(status, result.get("message", "error"))
                return status, {}, result

    raise RestError(404, "Not Found")
