# store.py — the forge's in-memory store: one viewer, repositories, git objects,
# refs, issues, pull requests, labels and comments.
#
# Auth model: the entry knows one bearer token (MAKEFUN_SHOP_TOKEN). The login
# that token names is NOT known in advance (the suite's scenario derives it
# from its seed), so the store learns it from the first authenticated request
# that names an owner: the suite always names the viewer first (the git group
# runs first and only ever names the viewer), and any other login after that
# is a stranger whose repositories do not exist (404, never 403).
#
# Determinism: no wall clock, no randomness. A logical clock (fixed epoch,
# +1 per event) stamps every server-built commit and merged_at, so replaying
# the same requests leaves byte-identical state.

import base64
import hashlib
import threading
import time

import gitobj

IDENTITY = "Conformance <conformance@make.fun>"
CLOCK_EPOCH = 1700000000
DEFAULT_BRANCH = "main"

# Global (Relay) ids: base64 of a type-prefixed key — opaque, stable, and
# decodable for node(id:). Never the plain database id or issue number.


def global_id(kind, key):
    raw = ("%s:%s" % (kind, key)).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def parse_global_id(value):
    try:
        raw = base64.b64decode(value.encode("ascii"), validate=True).decode("utf-8")
    except Exception:
        return None
    if ":" not in raw:
        return None
    kind, key = raw.split(":", 1)
    return kind, key


def iso_of(timestamp):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp))


class ExistsError(Exception):
    pass


class ConflictError(Exception):
    """A 409: the caller's sha does not match the current blob."""
    pass


class NotFoundError(Exception):
    pass


class Issue:
    __slots__ = ("number", "gid", "title", "body", "state", "labels",
                 "comments", "seq", "repo")

    def __init__(self, number, gid, title, body, repo, seq):
        self.number = number
        self.gid = gid
        self.title = title
        self.body = body
        self.state = "open"
        self.labels = []
        self.comments = []
        self.seq = seq
        self.repo = repo


class Comment:
    __slots__ = ("cid", "gid", "body", "seq")

    def __init__(self, cid, gid, body, seq):
        self.cid = cid
        self.gid = gid
        self.body = body
        self.seq = seq


class PullRequest:
    __slots__ = ("number", "gid", "title", "body", "state", "head", "base",
                 "head_sha", "base_sha", "merged", "merged_at",
                 "merge_commit_sha", "seq", "repo")

    def __init__(self, number, gid, title, body, head, base, head_sha, base_sha, repo, seq):
        self.number = number
        self.gid = gid
        self.title = title
        self.body = body
        self.state = "open"
        self.head = head
        self.base = base
        self.head_sha = head_sha
        self.base_sha = base_sha
        self.merged = False
        self.merged_at = None
        self.merge_commit_sha = None
        self.seq = seq
        self.repo = repo


class Repo:
    def __init__(self, name, description, private, database_id, owner, seq):
        self.name = name
        self.description = description
        self.private = private
        self.database_id = database_id
        self.owner = owner
        self.default_branch = DEFAULT_BRANCH
        self.seq = seq
        self.objects = {}       # oid -> (type, data)
        self.refs = {}          # "refs/heads/main" -> oid
        self.labels = {}        # name -> {"name","color"}
        self.issues = {}        # number -> Issue
        self.pulls = {}         # number -> PullRequest
        self.next_number = 1    # shared issue/PR numbering, GitHub-like
        self._json_cache = {}   # (kind, key) -> cached serialized bytes

    # -- objects ------------------------------------------------------
    def get_object(self, oid):
        return self.objects.get(oid)

    def has_object(self, oid):
        return oid in self.objects

    def put_object(self, obj_type, data):
        oid = gitobj.hash_object(obj_type, data)
        if oid not in self.objects:
            self.objects[oid] = (obj_type, data)
        return oid

    def put_objects(self, items):
        for obj_type, data in items:
            self.put_object(obj_type, data)

    # -- refs ---------------------------------------------------------
    def get_ref(self, ref):
        return self.refs.get(ref)

    def set_ref(self, ref, oid):
        self.refs[ref] = oid

    def delete_ref(self, ref):
        self.refs.pop(ref, None)

    def branch_tip(self, branch):
        return self.refs.get("refs/heads/" + branch)

    def all_refs(self):
        return dict(self.refs)

    # -- issues / pulls ------------------------------------------------
    def issue_by_number(self, number):
        return self.issues.get(number)

    def pull_by_number(self, number):
        return self.pulls.get(number)

    def open_pull_from(self, head, base):
        for pr in self.pulls.values():
            if pr.state == "open" and pr.head == head and pr.base == base:
                return pr
        return None

    def add_label(self, name, color):
        label = {"name": name, "color": color}
        self.labels[name] = label
        return label


class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.viewer_login = None
        self.repos = {}          # name -> Repo (the viewer's repositories)
        self.next_repo_db_id = 1
        self.next_global_id = 1
        self.next_comment_id = 1
        self.clock = CLOCK_EPOCH
        self.seq = 0             # creation-order sequence (sort keys)
        self.request_seq = 0     # X-GitHub-Request-Id counter

    # -- deterministic time / ids --------------------------------------
    def tick(self):
        self.clock += 1
        return self.clock

    def next_seq(self):
        self.seq += 1
        return self.seq

    def next_request_id(self):
        self.request_seq += 1
        return "req-%08d" % self.request_seq

    def new_global_id(self):
        gid = self.next_global_id
        self.next_global_id += 1
        return gid

    def new_comment_id(self):
        cid = self.next_comment_id
        self.next_comment_id += 1
        return cid

    # -- viewer ---------------------------------------------------------
    def resolve_owner(self, owner):
        """Learn/validate the token owner's login.

        Returns the owner when it is the viewer (learning it on first sight),
        else None (a stranger: nothing under that login exists)."""
        if owner is None:
            return None
        if self.viewer_login is None:
            self.viewer_login = owner
            for repo in self.repos.values():
                repo.owner = owner
            return owner
        return owner if owner == self.viewer_login else None

    def viewer(self):
        return self.viewer_login

    # -- repositories ---------------------------------------------------
    def create_repo(self, name, private, description):
        if name in self.repos:
            raise ExistsError("repository %s already exists" % name)
        repo = Repo(name, description, bool(private), self.next_repo_db_id,
                    self.viewer_login, self.next_seq())
        self.next_repo_db_id += 1
        self.repos[name] = repo
        return repo

    def get_repo(self, name):
        return self.repos.get(name)

    def delete_repo(self, name):
        return self.repos.pop(name, None) is not None

    def list_repos(self):
        return sorted(self.repos.values(), key=lambda r: r.seq)

    # -- git helpers ------------------------------------------------------
    def is_ancestor(self, repo, ancestor_oid, descendant_oid):
        """True when ancestor_oid is reachable from descendant_oid (or equal)."""
        if ancestor_oid == descendant_oid:
            return True
        if not gitobj.is_oid(ancestor_oid) or not gitobj.is_oid(descendant_oid):
            return False
        seen = set()
        stack = [descendant_oid]
        while stack:
            oid = stack.pop()
            if oid == ancestor_oid:
                return True
            if oid in seen:
                continue
            seen.add(oid)
            obj = repo.get_object(oid)
            if obj is None or obj[0] != "commit":
                continue
            stack.extend(gitobj.parse_commit(obj[1])["parents"])
        return False

    def merge_base(self, repo, oid_a, oid_b):
        """A merge-base of two commits: the newest common ancestor by clock order."""
        if oid_a == oid_b:
            return oid_a
        # Collect ancestors of a with their depth.
        depth_a = {}
        stack = [(oid_a, 0)]
        while stack:
            oid, d = stack.pop()
            if oid in depth_a:
                continue
            depth_a[oid] = d
            obj = repo.get_object(oid)
            if obj is None or obj[0] != "commit":
                continue
            for p in gitobj.parse_commit(obj[1])["parents"]:
                stack.append((p, d + 1))
        # Walk b's ancestors; the first one also in a (shallowest) is a base.
        best = None
        best_depth = None
        stack = [(oid_b, 0)]
        seen = set()
        while stack:
            oid, d = stack.pop()
            if oid in seen:
                continue
            seen.add(oid)
            if oid in depth_a:
                total = d + depth_a[oid]
                if best_depth is None or total < best_depth:
                    best, best_depth = oid, total
            obj = repo.get_object(oid)
            if obj is None or obj[0] != "commit":
                continue
            for p in gitobj.parse_commit(obj[1])["parents"]:
                stack.append((p, d + 1))
        return best

    def reachable_commits(self, repo, tips, depth=None):
        """Commits reachable from tips within depth generations (None = all).

        Depth counts generations from a tip: depth 1 is the tips alone.
        Returns {oid: distance}."""
        out = {}
        stack = [(t, 0) for t in tips if gitobj.is_oid(t)]
        while stack:
            oid, d = stack.pop()
            if oid in out:
                continue
            if depth is not None and d >= depth:
                continue
            obj = repo.get_object(oid)
            if obj is None or obj[0] != "commit":
                continue
            out[oid] = d
            for p in gitobj.parse_commit(obj[1])["parents"]:
                stack.append((p, d + 1))
        return out

    def commit_files(self, repo, commit_oid):
        """{path: (mode, blob_oid)} for a commit's tree (empty when no commit)."""
        if not commit_oid:
            return {}
        obj = repo.get_object(commit_oid)
        if obj is None or obj[0] != "commit":
            return {}
        tree_oid = gitobj.parse_commit(obj[1])["tree"]
        return gitobj.flatten_tree(repo.get_object, tree_oid)

    def make_commit(self, repo, files, message, parents):
        """Create a commit over a flat {path: (mode, blob_oid)} file map.

        Returns (commit_oid, {oid: (type, data)} of every object created)."""
        created = {}
        root_oid, trees = gitobj.build_trees_from_files(files)
        created.update(trees)
        ts = self.tick()
        who = "%s %d +0000" % (IDENTITY, ts)
        data = gitobj.build_commit(root_oid, parents, who, who, message)
        commit_oid = gitobj.hash_object("commit", data)
        created[commit_oid] = ("commit", data)
        repo.put_objects(list(created.values()))
        return commit_oid, created

    # -- contents API -----------------------------------------------------
    def commit_file(self, repo, branch, path, content, message, expected_sha=None):
        """Create or update one file through the contents API.

        branch: None means the default branch. A branch that does not exist is
        created from the default branch tip (the MCP setup relies on this).
        expected_sha: the blob sha the caller believes is current; a mismatch
        is a 409. Updating without a sha is a 422; creating with one is a 409.
        Returns (commit_oid, blob_oid, status) with status 201 (create) or 200.
        """
        branch = branch or repo.default_branch
        ref = "refs/heads/" + branch
        tip = repo.get_ref(ref)
        if tip is None:
            if repo.refs:
                base_tip = repo.get_ref("refs/heads/" + repo.default_branch)
                if base_tip is None:
                    raise NotFoundError("no default branch to fork")
                tip = base_tip
                # The branch is created from the default tip below.
            else:
                tip = None
        files = self.commit_files(repo, tip)
        blob_oid = gitobj.hash_object("blob", content)
        existed = path in files
        if existed:
            current_sha = files[path][1]
            if expected_sha is None:
                raise ExistsError("file %s already exists" % path)
            if expected_sha.lower() != current_sha.lower():
                raise ConflictError("sha %s is stale; current is %s" % (expected_sha, current_sha))
            status = 200
        else:
            if expected_sha is not None:
                raise ConflictError("file %s does not exist" % path)
            status = 201
        if not existed or files[path][1] != blob_oid:
            repo.put_object("blob", content)
        files[path] = ("100644", blob_oid)
        parents = [tip] if tip else []
        commit_oid, _created = self.make_commit(repo, files, message, parents)
        repo.set_ref(ref, commit_oid)
        return commit_oid, blob_oid, status

    def delete_file(self, repo, branch, path, message, expected_sha=None):
        branch = branch or repo.default_branch
        ref = "refs/heads/" + branch
        tip = repo.get_ref(ref)
        if tip is None:
            raise NotFoundError("branch %s does not exist" % branch)
        files = self.commit_files(repo, tip)
        if path not in files:
            raise NotFoundError("file %s does not exist" % path)
        if expected_sha is not None and expected_sha.lower() != files[path][1].lower():
            raise ConflictError("sha %s is stale" % expected_sha)
        del files[path]
        commit_oid, _created = self.make_commit(repo, files, message, [tip])
        repo.set_ref(ref, commit_oid)
        return commit_oid

    # -- squash merge -------------------------------------------------------
    def squash_merge(self, repo, pr):
        """Squash-merge a pull request onto its base branch.

        Returns (merge_commit_oid, conflict_bool). On conflict nothing changes.
        The result tree is a file-level three-way merge of base + head over the
        merge-base; a path both sides changed differently is a conflict.
        """
        base_ref = "refs/heads/" + pr.base
        head_ref = "refs/heads/" + pr.head
        base_tip = repo.get_ref(base_ref)
        head_tip = repo.get_ref(head_ref)
        if base_tip is None or head_tip is None:
            raise NotFoundError("head or base branch is missing")
        base_files = self.commit_files(repo, base_tip)
        head_files = self.commit_files(repo, head_tip)
        mb = self.merge_base(repo, base_tip, head_tip)
        mb_files = self.commit_files(repo, mb) if mb else {}
        result = dict(base_files)
        conflict = False
        for path, entry in head_files.items():
            base_entry = base_files.get(path)
            mb_entry = mb_files.get(path)
            if base_entry != mb_entry and entry != mb_entry and base_entry != entry:
                conflict = True
            elif entry != mb_entry:
                result[path] = entry
        for path in list(mb_files):
            if path not in head_files and path in base_files:
                # Deleted on head; keep the base version unless base also changed it.
                if base_files.get(path) == mb_files[path]:
                    result.pop(path, None)
        if conflict:
            return None, True
        message = pr.title
        commit_oid, _created = self.make_commit(repo, result, message, [base_tip])
        repo.set_ref(base_ref, commit_oid)
        pr.merged = True
        pr.merged_at = iso_of(self.tick())
        pr.merge_commit_sha = commit_oid
        pr.state = "closed"
        return commit_oid, False

    def pr_mergeable(self, repo, pr):
        """MERGEABLE / CONFLICTING / UNKNOWN for a pull request."""
        base_tip = repo.get_ref("refs/heads/" + pr.base)
        head_tip = repo.get_ref("refs/heads/" + pr.head)
        if base_tip is None or head_tip is None:
            return "UNKNOWN"
        if pr.state != "open":
            return "UNKNOWN"
        base_files = self.commit_files(repo, base_tip)
        head_files = self.commit_files(repo, head_tip)
        mb = self.merge_base(repo, base_tip, head_tip)
        mb_files = self.commit_files(repo, mb) if mb else {}
        for path, entry in head_files.items():
            base_entry = base_files.get(path)
            mb_entry = mb_files.get(path)
            if base_entry != mb_entry and entry != mb_entry and base_entry != entry:
                return "CONFLICTING"
        return "MERGEABLE"

    # -- issues / pulls / comments ----------------------------------------
    def create_issue(self, repo, title, body, labels=None):
        """Returns (201, Issue) or (422, {"message": ...}) for an unknown label."""
        labels = list(labels or [])
        for name in labels:
            if name not in repo.labels:
                return 422, {"message": "label %s does not exist" % name}
        number = repo.next_number
        repo.next_number += 1
        issue = Issue(number, self.new_global_id(), title, body, repo,
                      self.next_seq())
        issue.labels = labels
        repo.issues[number] = issue
        return 201, issue

    def set_issue_state(self, repo, number, state):
        issue = repo.issue_by_number(number)
        if issue is None:
            return None
        issue.state = state
        return issue

    def add_comment(self, repo, issue, body):
        comment = Comment(self.new_comment_id(), self.new_global_id(), body,
                          self.next_seq())
        issue.comments.append(comment)
        return comment

    def create_pull(self, repo, head, base, title, body):
        """Returns (201, PullRequest) or (422, {"message": ...})."""
        head_tip = repo.branch_tip(head)
        base_tip = repo.branch_tip(base)
        if head_tip is None or base_tip is None:
            return 422, {"message": "head or base branch does not exist"}
        if repo.open_pull_from(head, base) is not None:
            return 422, {"message": "a pull request from %s to %s is already open" % (head, base)}
        number = repo.next_number
        repo.next_number += 1
        pr = PullRequest(number, self.new_global_id(), title, body, head, base,
                         head_tip, base_tip, repo, self.next_seq())
        repo.pulls[number] = pr
        return 201, pr

    def merge_pull(self, repo, pr, method="squash"):
        """Returns (200, {"merged": True, "sha": ..., "message": ...}),
        (404, ...) or (405, ...) for a conflict / a non-open PR."""
        if pr is None:
            return 404, {"message": "Not Found"}
        if pr.state != "open":
            return 405, {"message": "pull request is not open"}
        try:
            merge_oid, conflict = self.squash_merge(repo, pr)
        except NotFoundError as err:
            return 404, {"message": str(err)}
        if conflict:
            return 405, {"message": "merge conflict"}
        return 200, {"merged": True, "sha": merge_oid, "message": pr.title}

    def pr_files(self, repo, pr):
        """[{filename, status, additions, deletions, changes}] for a PR,
        from a line diff of the base tip tree against the head tip tree."""
        base_files = self.commit_files(repo, repo.branch_tip(pr.base))
        head_files = self.commit_files(repo, repo.branch_tip(pr.head))
        out = []
        for path in sorted(set(base_files) | set(head_files)):
            base_entry = base_files.get(path)
            head_entry = head_files.get(path)
            if base_entry == head_entry:
                continue
            if base_entry is None:
                status = "added"
                old_lines = []
                new_lines = self._blob_lines(repo, head_entry[1])
            elif head_entry is None:
                status = "removed"
                old_lines = self._blob_lines(repo, base_entry[1])
                new_lines = []
            else:
                status = "modified"
                old_lines = self._blob_lines(repo, base_entry[1])
                new_lines = self._blob_lines(repo, head_entry[1])
            additions, deletions = self._line_diff(old_lines, new_lines)
            out.append({"filename": path, "status": status,
                        "additions": additions, "deletions": deletions,
                        "changes": additions + deletions})
        return out

    def _blob_lines(self, repo, blob_oid):
        obj = repo.get_object(blob_oid)
        if obj is None or obj[0] != "blob":
            return []
        return obj[1].decode("utf-8", "replace").splitlines()

    @staticmethod
    def _line_diff(old, new):
        import difflib
        matcher = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
        additions = deletions = 0
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag in ("insert", "replace"):
                additions += j2 - j1
            if tag in ("delete", "replace"):
                deletions += i2 - i1
        return additions, deletions
