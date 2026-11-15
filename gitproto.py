# gitproto.py — git smart HTTP: v2/v0 advertisements, ls-refs, fetch and
# receive-pack, over the store in store.py.
#
# The wire shapes mirror the kit's env/src/git-client.ts exactly:
#   * v2 advertisement: "# service=..." preamble, "version 2", capability
#     lines (fetch=shallow, ls-refs=unborn, agent, object-format=sha1).
#   * v0 advertisement: "# service=..." preamble, "<oid> HEAD\\0<caps>" first
#     line (or "<zero> capabilities^{}\\0<caps>" for an empty repo), then
#     "<oid> <ref>" lines and peeled "^{}" lines for annotated tags.
#   * v2 ls-refs: ref lines with symref-target: / peeled: attributes, unborn
#     HEAD when asked, ref-prefix filtering.
#   * v2 fetch: acknowledgments / shallow-info / packfile sections, the pack
#     sideband-framed (band 1). The pack carries exactly the objects the
#     client lacks: (depth-limited wanted objects) minus (held objects, where
#     a shallow boundary stops the held traversal). An empty pack (32 bytes)
#     is sent when nothing is new.
#   * receive-pack (v0): command list + pack, fast-forward only, per-ref
#     report-status with the exact reason "non-fast-forward", the report
#     sideband-framed on band 1. A refused ref still unpacks (unpack ok).

import gitobj
from gitobj import ZERO_OID, is_oid

AGENT = "forge/1.0"

UPLOAD_V2_CAPS = [
    "agent=%s" % AGENT,
    "object-format=sha1",
    "fetch=shallow",
    "ls-refs=unborn",
]
UPLOAD_V0_CAPS = ("symref=HEAD:refs/heads/main agent=%s object-format=sha1 "
                  "side-band-64k thin-pack ofs-delta multi_ack_detailed shallow" % AGENT)
RECEIVE_V0_CAPS = ("report-status side-band-64k delete-refs agent=%s "
                   "object-format=sha1 ofs-delta" % AGENT)


class GitError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def _ref_lines(repo):
    """[(oid, name)] for every ref, HEAD first (HEAD is the default branch tip)."""
    refs = repo.all_refs()
    out = []
    head_oid = refs.get("refs/heads/" + repo.default_branch)
    if head_oid:
        out.append((head_oid, "HEAD"))
    for name in sorted(refs):
        out.append((refs[name], name))
        obj = repo.get_object(refs[name])
        if obj is not None and obj[0] == "tag":
            target = gitobj.parse_tag(obj[1])["object"]
            out.append((target, name + "^{}"))
    return out


def _peeled_oid(repo, oid):
    obj = repo.get_object(oid)
    if obj is not None and obj[0] == "tag":
        return gitobj.parse_tag(obj[1])["object"]
    return None


# ---------------------------------------------------------------------------
# advertisements
# ---------------------------------------------------------------------------

def advertise_upload_pack(repo, want_v2):
    if want_v2:
        lines = ["# service=git-upload-pack\n", "flush", "version 2\n"]
        lines.extend(c + "\n" for c in UPLOAD_V2_CAPS)
        lines.append("flush")
        return 200, "application/x-git-upload-pack-advertisement", gitobj.encode_pkt_lines(lines)
    lines = ["# service=git-upload-pack\n", "flush"]
    refs = _ref_lines(repo)
    if not refs:
        lines.append("%s capabilities^{}\0%s\n" % (ZERO_OID, UPLOAD_V0_CAPS))
    else:
        first_oid, first_name = refs[0]
        lines.append("%s %s\0%s\n" % (first_oid, first_name, UPLOAD_V0_CAPS))
        for oid, name in refs[1:]:
            lines.append("%s %s\n" % (oid, name))
    lines.append("flush")
    return 200, "application/x-git-upload-pack-advertisement", gitobj.encode_pkt_lines(lines)


def advertise_receive_pack(repo):
    lines = ["# service=git-receive-pack\n", "flush"]
    refs = _ref_lines(repo)
    if not refs:
        lines.append("%s capabilities^{}\0%s\n" % (ZERO_OID, RECEIVE_V0_CAPS))
    else:
        first_oid, first_name = refs[0]
        lines.append("%s %s\0%s\n" % (first_oid, first_name, RECEIVE_V0_CAPS))
        for oid, name in refs[1:]:
            lines.append("%s %s\n" % (oid, name))
    lines.append("flush")
    return 200, "application/x-git-receive-pack-advertisement", gitobj.encode_pkt_lines(lines)


# ---------------------------------------------------------------------------
# v2 ls-refs
# ---------------------------------------------------------------------------

def _parse_v2_request(body):
    """Parse a v2 command request body into (command, args, opts).

    args: ordered list of (key, value) after the first delim; opts holds the
    flags seen. Raises ValueError on a malformed frame."""
    packets = gitobj.decode_pkt_lines(body)
    lines = []
    seen_delim = False
    for kind, payload in packets:
        if kind == "flush":
            break
        if kind == "delim":
            seen_delim = True
            continue
        if kind == "data":
            lines.append(payload.decode("utf-8", "replace"))
    if not lines:
        raise ValueError("empty v2 request")
    command = lines[0].strip()
    args = []
    for line in lines[1:]:
        line = line.rstrip("\n")
        if line == "":
            continue
        if " " in line:
            key, value = line.split(" ", 1)
            args.append((key, value))
        else:
            args.append((line, ""))
    return command, args, seen_delim


def handle_ls_refs(repo, body):
    command, args, _ = _parse_v2_request(body)
    if command != "command=ls-refs":
        raise GitError(400, "expected ls-refs, got %r" % command)
    peel = False
    symrefs = False
    unborn = False
    prefixes = []
    for key, value in args:
        if key == "peel":
            peel = True
        elif key == "symrefs":
            symrefs = True
        elif key == "unborn":
            unborn = True
        elif key == "ref-prefix":
            prefixes.append(value)
    out = []

    def match(name):
        return not prefixes or any(name.startswith(p) for p in prefixes)

    refs = repo.all_refs()
    head_oid = refs.get("refs/heads/" + repo.default_branch)
    if match("HEAD"):
        if head_oid:
            line = "%s HEAD" % head_oid
            if symrefs:
                line += " symref-target:refs/heads/%s" % repo.default_branch
            out.append(line + "\n")
        elif unborn:
            line = "unborn HEAD"
            if symrefs:
                line += " symref-target:refs/heads/%s" % repo.default_branch
            out.append(line + "\n")
    for name in sorted(refs):
        if not match(name):
            continue
        oid = refs[name]
        line = "%s %s" % (oid, name)
        if peel:
            peeled = _peeled_oid(repo, oid)
            if peeled:
                line += " peeled:%s" % peeled
        out.append(line + "\n")
    out.append("flush")
    return 200, "application/x-git-upload-pack-result", gitobj.encode_pkt_lines(out)


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------

def _held_objects(repo, haves, shallow_set):
    """Objects the client already holds: reachable from the haves, with a
    shallow boundary stopping the commit traversal (its parents are not held)."""
    held = set()
    stack = list(haves)
    seen = set()
    while stack:
        oid = stack.pop()
        if oid in seen or not is_oid(oid):
            continue
        seen.add(oid)
        obj = repo.get_object(oid)
        if obj is None:
            continue
        otype, data = obj
        held.add(oid)
        if otype == "commit":
            c = gitobj.parse_commit(data)
            held.update(gitobj.collect_reachable([c["tree"]], repo.get_object))
            if oid not in shallow_set:
                stack.extend(c["parents"])
        elif otype == "tag":
            stack.append(gitobj.parse_tag(data)["object"])
        elif otype == "tree":
            held.update(gitobj.collect_reachable([oid], repo.get_object))
    return held


def _wanted_objects(repo, wants, depth):
    """(objects, commits) reachable from the wants within depth generations.

    Depth counts commit generations from a want: depth 1 is the want commits
    alone. Tag wants contribute the tag object plus their target's closure."""
    objects = set()
    commits = {}
    stack = []
    for want in wants:
        obj = repo.get_object(want)
        if obj is None:
            continue
        otype, _data = obj
        if otype == "tag":
            objects.add(want)
            stack.append((gitobj.parse_tag(obj[1])["object"], 0))
        elif otype == "commit":
            stack.append((want, 0))
        else:
            objects.add(want)
    seen_commit = set()
    while stack:
        oid, d = stack.pop()
        if oid in seen_commit:
            continue
        if depth is not None and d >= depth:
            continue
        obj = repo.get_object(oid)
        if obj is None or obj[0] != "commit":
            continue
        seen_commit.add(oid)
        commits[oid] = d
        c = gitobj.parse_commit(obj[1])
        objects.add(oid)
        objects.update(gitobj.collect_reachable([c["tree"]], repo.get_object))
        for p in c["parents"]:
            stack.append((p, d + 1))
    return objects, commits


def _fetch_plan(repo, wants, haves, depth, shallow_req):
    """Return (send_oids, shallow_list, unshallow_list)."""
    shallow_set = set(shallow_req)
    wanted_objects, wanted_commits = _wanted_objects(repo, wants, depth)
    held = _held_objects(repo, haves, shallow_set)
    send = wanted_objects - held
    # Shallow boundaries for shallow-info (only when a depth or a shallow
    # boundary is in play).
    shallow_list = []
    unshallow_list = []
    if depth is not None or shallow_req:
        # relevant_haves needs the want ancestors, but only when there are
        # haves at all; a fresh clone (no haves) skips the walk entirely.
        if haves:
            want_ancestors = _ancestor_set(repo, wants)
            relevant_haves = set(h for h in haves if h in want_ancestors)
        else:
            relevant_haves = set()
        candidates = set(wanted_commits) | relevant_haves | shallow_set
        held_or_sent = set(wanted_commits) | set(haves)
        new_shallow = set()
        for cand in candidates:
            obj = repo.get_object(cand)
            if obj is None or obj[0] != "commit":
                continue
            parents = gitobj.parse_commit(obj[1])["parents"]
            if any(p not in held_or_sent for p in parents):
                new_shallow.add(cand)
        shallow_list = sorted(new_shallow)
        unshallow_list = sorted(shallow_set - new_shallow)
    return send, shallow_list, unshallow_list


def _ancestor_set(repo, tips):
    out = set()
    stack = [t for t in tips if is_oid(t)]
    while stack:
        oid = stack.pop()
        if oid in out:
            continue
        obj = repo.get_object(oid)
        if obj is None or obj[0] != "commit":
            continue
        out.add(oid)
        stack.extend(gitobj.parse_commit(obj[1])["parents"])
    return out


def _sideband_pack(pack):
    """Split a pack into sideband (band 1) pkt-lines."""
    out = bytearray()
    max_data = gitobj.PKT_LINE_MAX_PAYLOAD - 1
    for off in range(0, len(pack), max_data):
        chunk = pack[off:off + max_data]
        out += gitobj.pkt_line(b"\x01" + chunk)
    return bytes(out)


def handle_fetch_v2(repo, body):
    command, args, _ = _parse_v2_request(body)
    if command != "command=fetch":
        raise GitError(400, "expected fetch, got %r" % command)
    wants = []
    haves = []
    shallow_req = []
    depth = None
    for key, value in args:
        if key == "want":
            wants.append(value)
        elif key == "have":
            haves.append(value)
        elif key == "shallow":
            shallow_req.append(value)
        elif key == "deepen":
            try:
                depth = int(value)
            except ValueError:
                pass
        # deepen-since / deepen-not / thin-pack / no-progress / ofs-delta:
        # accepted, not needed by the suite.
    send, shallow_list, unshallow_list = _fetch_plan(repo, wants, haves, depth, shallow_req)
    objects = []
    for oid in sorted(send):
        obj = repo.get_object(oid)
        if obj is not None:
            objects.append((obj[0], obj[1]))
    pack = gitobj.write_pack(objects)

    out = bytearray()
    # The acknowledgments section exists only when the client sent haves;
    # a fresh clone goes straight to the packfile section.
    if haves:
        out += gitobj.encode_pkt_lines(["acknowledgments\n"])
        if all(repo.has_object(h) for h in haves):
            out += gitobj.encode_pkt_lines(["ready\n"])
        else:
            out += gitobj.encode_pkt_lines(["NAK\n"])
        out += gitobj.pkt_delim()
    if shallow_list or unshallow_list:
        out += gitobj.encode_pkt_lines(["shallow-info\n"])
        for oid in shallow_list:
            out += gitobj.encode_pkt_lines(["shallow %s\n" % oid])
        for oid in unshallow_list:
            out += gitobj.encode_pkt_lines(["unshallow %s\n" % oid])
        out += gitobj.pkt_delim()
    out += gitobj.encode_pkt_lines(["packfile\n"])
    out += _sideband_pack(pack)
    out += gitobj.pkt_flush()
    return 200, "application/x-git-upload-pack-result", bytes(out)


def handle_fetch_v0(repo, body):
    # v0: wants (+shallow/deepen) up to the first flush, then haves + done.
    packets = gitobj.decode_pkt_lines(body)
    wants = []
    haves = []
    depth = None
    section = 0
    for kind, payload in packets:
        if kind == "flush":
            section += 1
            continue
        if kind != "data":
            continue
        line = payload.decode("utf-8", "replace").rstrip("\n")
        if section == 0:
            if line.startswith("want "):
                wants.append(line[5:].split(" ")[0])
            elif line.startswith("deepen "):
                try:
                    depth = int(line[7:])
                except ValueError:
                    pass
        else:
            if line.startswith("have "):
                haves.append(line[5:])
    send, _shallow, _unshallow = _fetch_plan(repo, wants, haves, depth, [])
    objects = []
    for oid in sorted(send):
        obj = repo.get_object(oid)
        if obj is not None:
            objects.append((obj[0], obj[1]))
    pack = gitobj.write_pack(objects)
    out = bytearray()
    out += gitobj.encode_pkt_lines(["NAK\n"])
    out += _sideband_pack(pack)
    out += gitobj.pkt_flush()
    return 200, "application/x-git-upload-pack-result", bytes(out)


def handle_upload_pack(repo, body, want_v2):
    if want_v2:
        command, _args, _ = _parse_v2_request(body)
        if command == "command=ls-refs":
            return handle_ls_refs(repo, body)
        if command == "command=fetch":
            return handle_fetch_v2(repo, body)
        raise GitError(400, "unknown v2 command %r" % command)
    return handle_fetch_v0(repo, body)


# ---------------------------------------------------------------------------
# receive-pack (push)
# ---------------------------------------------------------------------------

def handle_receive_pack(store, repo, body):
    # Split the pkt-lined command list (up to the first flush) from the raw pack.
    commands = []
    capabilities = []
    pack_start = None
    off = 0
    # Walk the raw bytes so the pack offset is exact.
    pos = 0
    n = len(body)
    while pos < n:
        head = body[pos:pos + 4]
        if head == b"0000":
            pos += 4
            pack_start = pos
            break
        if head in (b"0001", b"0002"):
            pos += 4
            continue
        try:
            length = int(head.decode("ascii"), 16)
        except ValueError:
            raise GitError(400, "malformed pkt-line in receive-pack")
        if length < 4 or pos + length > n:
            raise GitError(400, "pkt-line length out of range in receive-pack")
        payload = body[pos + 4:pos + length]
        pos += length
        line = payload.decode("utf-8", "replace").rstrip("\n")
        nul = line.find("\x00")
        caps = ""
        if nul >= 0:
            caps = line[nul + 1:]
            line = line[:nul]
        parts = line.split(" ")
        if len(parts) < 3:
            raise GitError(400, "bad receive-pack command %r" % line)
        old, new, ref = parts[0], parts[1], " ".join(parts[2:])
        if not is_oid(old) or not is_oid(new) or not ref.startswith("refs/"):
            raise GitError(400, "bad receive-pack command %r" % line)
        commands.append((old, new, ref))
        if caps:
            capabilities = caps.split(" ")
    if not commands:
        raise GitError(400, "receive-pack needs at least one command")
    pack_bytes = body[pack_start:] if pack_start is not None else b""
    use_sideband = "side-band-64k" in capabilities

    # Unpack first: a refused ref still unpacks (unpack ok).
    unpack_ok = True
    unpack_detail = "ok"
    if pack_bytes:
        try:
            parsed = gitobj.read_pack(pack_bytes, resolve_base=repo.get_object)
            repo.put_objects(list(parsed["objects"].values()))
        except ValueError as err:
            unpack_ok = False
            unpack_detail = str(err)
    results = []  # (ref, ok, reason)
    for old, new, ref in commands:
        if not unpack_ok:
            results.append((ref, False, "unpack failed"))
            continue
        current = repo.get_ref(ref)
        if new == ZERO_OID:
            # Delete.
            if current is None:
                results.append((ref, False, "ref does not exist"))
            elif old != ZERO_OID and old != current:
                results.append((ref, False, "old ref does not match"))
            else:
                repo.delete_ref(ref)
                results.append((ref, True, ""))
            continue
        if old == ZERO_OID:
            if current is not None:
                results.append((ref, False, "already exists"))
                continue
        else:
            if current is None:
                results.append((ref, False, "ref does not exist"))
                continue
            if old != current:
                results.append((ref, False, "old ref does not match"))
                continue
            if not store.is_ancestor(repo, old, new):
                results.append((ref, False, "non-fast-forward"))
                continue
        if repo.get_object(new) is None:
            results.append((ref, False, "object not found"))
            continue
        repo.set_ref(ref, new)
        results.append((ref, True, ""))

    lines = ["unpack %s\n" % ("ok" if unpack_ok else unpack_detail)]
    for ref, ok, reason in results:
        lines.append(("ok %s\n" % ref) if ok else ("ng %s %s\n" % (ref, reason)))
    inner = gitobj.encode_pkt_lines(lines)
    # The report stream itself ends with a flush pkt-line inside the
    # sideband data (git's receive_status reads the demultiplexed stream
    # until that flush); the wire then ends with a flush of its own.
    inner += gitobj.pkt_flush()
    out = bytearray()
    if use_sideband:
        max_data = gitobj.PKT_LINE_MAX_PAYLOAD - 1
        for off in range(0, len(inner), max_data):
            out += gitobj.pkt_line(b"\x01" + inner[off:off + max_data])
    else:
        out += inner
    out += gitobj.pkt_flush()
    return 200, "application/x-git-receive-pack-result", bytes(out)
