# gitobj.py — git objects, packfiles and pkt-lines (from-scratch, stdlib only).
#
# Byte formats mirror the kit's env/src/git-client.ts exactly, so the suite's
# pack reader (readPack) and object checks (hash recomputation, byte-exact
# commit/tree/blob round-trips) accept what this module writes and reads.
#
# Object id: SHA-1 over "<type> <size>\0" + data.
# Pack: "PACK" + u32be version (2) + u32be count, then per object a type/size
# varint header + zlib-deflated data, then a 20-byte SHA-1 trailer over all
# preceding bytes. No deltas are ever written; OFS/REF deltas are resolved
# when reading (a thin pack's REF_DELTA base may come from the store).
# Pkt-line: 4 hex chars (length incl. the 4 header bytes) + payload;
# "0000" flush, "0001" delim, "0002" response-end.

import hashlib
import zlib

ZERO_OID = "0" * 40
PKT_LINE_MAX = 65520
PKT_LINE_MAX_PAYLOAD = PKT_LINE_MAX - 4

TYPE_CODE = {"commit": 1, "tree": 2, "blob": 3, "tag": 4, "ofs-delta": 6, "ref-delta": 7}
CODE_TYPE = {1: "commit", 2: "tree", 3: "blob", 4: "tag", 6: "ofs-delta", 7: "ref-delta"}

_HEX40 = set("0123456789abcdef")


def is_oid(value):
    return isinstance(value, str) and len(value) == 40 and all(c in _HEX40 for c in value)


def hash_object(obj_type, data):
    h = hashlib.sha1()
    h.update(("%s %d\0" % (obj_type, len(data))).encode("ascii"))
    h.update(data)
    return h.hexdigest()


def object_header(obj_type, size):
    return ("%s %d\0" % (obj_type, size)).encode("ascii")


# --------------------------------------------------------------------------
# trees / commits / tags
# --------------------------------------------------------------------------

def parse_tree(data):
    """Parse a tree object into [(mode, name, oid)] in stored order."""
    entries = []
    off = 0
    n = len(data)
    while off < n:
        sp = data.index(b" ", off)
        nul = data.index(b"\x00", sp + 1)
        if nul + 21 > n:
            raise ValueError("tree entry has a truncated object id")
        mode = data[off:sp].decode("ascii")
        name = data[sp + 1:nul].decode("utf-8")
        oid = data[nul + 1:nul + 21].hex()
        entries.append((mode, name, oid))
        off = nul + 21
    return entries


def _tree_sort_key(entry):
    mode, name, _oid = entry
    # git's tree sort: a directory compares as if its name ended in '/'.
    key = (name + "/") if mode == "40000" else name
    return key.encode("utf-8")


def build_tree(entries):
    """Build a tree object from [(mode, name, oid)]; sorted git's way."""
    out = bytearray()
    for mode, name, oid in sorted(entries, key=_tree_sort_key):
        out += ("%s %s\0" % (mode, name)).encode("utf-8")
        out += bytes.fromhex(oid)
    return bytes(out)


def parse_commit(data):
    """Parse a commit object. Returns dict(tree, parents, author, committer, message)."""
    blank = data.find(b"\n\n")
    header_end = len(data) if blank < 0 else blank
    message = b"" if blank < 0 else data[blank + 2:]
    tree = ""
    parents = []
    author = ""
    committer = ""
    for line in data[:header_end].decode("utf-8", "replace").split("\n"):
        if not line:
            continue
        sp = line.find(" ")
        key = line if sp < 0 else line[:sp]
        value = "" if sp < 0 else line[sp + 1:]
        if key == "tree":
            tree = value
        elif key == "parent":
            parents.append(value)
        elif key == "author":
            author = value
        elif key == "committer":
            committer = value
    return {"tree": tree, "parents": parents, "author": author,
            "committer": committer, "message": message.decode("utf-8", "replace")}


def build_commit(tree, parents, author, committer, message):
    lines = ["tree %s" % tree]
    for p in parents:
        lines.append("parent %s" % p)
    lines.append("author %s" % author)
    lines.append("committer %s" % committer)
    lines.append("")
    return ("\n".join(lines) + "\n" + message).encode("utf-8")


def parse_tag(data):
    blank = data.find(b"\n\n")
    header_end = len(data) if blank < 0 else blank
    message = b"" if blank < 0 else data[blank + 2:]
    headers = {}
    for line in data[:header_end].decode("utf-8", "replace").split("\n"):
        if not line:
            continue
        sp = line.find(" ")
        if sp < 0:
            continue
        headers[line[:sp]] = line[sp + 1:]
    return {"object": headers.get("object", ""), "type": headers.get("type", ""),
            "tag": headers.get("tag", ""), "tagger": headers.get("tagger", ""),
            "message": message.decode("utf-8", "replace")}


def build_tag(obj, obj_type, tag, tagger, message):
    return ("object %s\ntype %s\ntag %s\ntagger %s\n\n%s" % (
        obj, obj_type, tag, tagger, message)).encode("utf-8")


def build_trees_from_files(files):
    """Build every tree object for a flat {path: (mode, oid)} map.

    Returns (root_tree_oid, {oid: (type, data)}) for all trees created.
    Paths are '/'-separated; intermediate directories get mode 40000.
    """
    # Build a nested dict: name -> ("file", mode, oid) | ("dir", {...})
    root = {}
    for path, (mode, oid) in files.items():
        parts = path.split("/")
        node = root
        for part in parts[:-1]:
            nxt = node.get(part)
            if nxt is None:
                nxt = ("dir", {})
                node[part] = nxt
            node = nxt[1]
        node[parts[-1]] = ("file", mode, oid)

    created = {}

    def build(node):
        entries = []
        for name, val in node.items():
            if val[0] == "file":
                entries.append((val[1], name, val[2]))
            else:
                sub_oid = build(val[1])
                entries.append(("40000", name, sub_oid))
        data = build_tree(entries)
        oid = hash_object("tree", data)
        created[oid] = ("tree", data)
        return oid

    root_oid = build(root)
    return root_oid, created


def flatten_tree(store_get, root_oid):
    """Return {path: (mode, oid)} for every blob under a tree (gitlinks skipped)."""
    out = {}

    def walk(oid, prefix):
        obj = store_get(oid)
        if obj is None:
            return
        otype, data = obj
        if otype != "tree":
            return
        for mode, name, entry_oid in parse_tree(data):
            path = prefix + name
            if mode == "40000":
                walk(entry_oid, path + "/")
            elif mode == "160000":
                continue
            else:
                out[path] = (mode, entry_oid)

    walk(root_oid, "")
    return out


# --------------------------------------------------------------------------
# pkt-lines
# --------------------------------------------------------------------------

def pkt_line(payload):
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    if len(payload) > PKT_LINE_MAX_PAYLOAD:
        raise ValueError("pkt-line payload too long")
    return ("%04x" % (len(payload) + 4)).encode("ascii") + payload


def pkt_flush():
    return b"0000"


def pkt_delim():
    return b"0001"


def encode_pkt_lines(items):
    out = bytearray()
    for item in items:
        if item == "flush":
            out += pkt_flush()
        elif item == "delim":
            out += pkt_delim()
        elif item == "response-end":
            out += b"0002"
        else:
            out += pkt_line(item)
    return bytes(out)


def decode_pkt_lines(data):
    """Decode a whole pkt-line stream into [('data', payload) | ('flush',) |
    ('delim',) | ('response-end',)]. Raises ValueError on a malformed frame."""
    packets = []
    off = 0
    n = len(data)
    while off < n:
        if off + 4 > n:
            raise ValueError("truncated pkt-line header")
        head = data[off:off + 4]
        if head == b"0000":
            packets.append(("flush", b""))
            off += 4
            continue
        if head == b"0001":
            packets.append(("delim", b""))
            off += 4
            continue
        if head == b"0002":
            packets.append(("response-end", b""))
            off += 4
            continue
        try:
            length = int(head.decode("ascii"), 16)
        except ValueError:
            raise ValueError("bad pkt-line length %r" % head)
        if length < 4 or off + length > n:
            raise ValueError("pkt-line length out of range")
        packets.append(("data", data[off + 4:off + length]))
        off += length
    return packets


# --------------------------------------------------------------------------
# packfiles
# --------------------------------------------------------------------------

def _write_pack_object_header(obj_type, size):
    code = TYPE_CODE[obj_type]
    out = bytearray()
    first = (code << 4) | (size & 0x0F)
    size >>= 4
    while size:
        out.append(first | 0x80)
        first = size & 0x7F
        size >>= 7
    out.append(first)
    return bytes(out)


def write_pack(objects):
    """Build a packfile from [(type, data)] plain objects (no deltas).

    Objects are written in the caller's order; callers sort by oid for
    determinism. The trailer is SHA-1 over everything before it.
    """
    parts = [b"PACK", (2).to_bytes(4, "big"), len(objects).to_bytes(4, "big")]
    for obj_type, data in objects:
        parts.append(_write_pack_object_header(obj_type, len(data)))
        parts.append(zlib.compress(data))
    body = b"".join(parts)
    return body + hashlib.sha1(body).digest()


def pack_checksum_ok(pack):
    if len(pack) < 20:
        return False
    return pack[-20:] == hashlib.sha1(pack[:-20]).digest()


def _read_varint_size(buf, start):
    if start >= len(buf):
        raise ValueError("pack entry has no size header")
    byte = buf[start]
    first = byte
    size = byte & 0x0F
    shift = 4
    off = start + 1
    while byte & 0x80:
        if off >= len(buf):
            raise ValueError("pack entry size varint is truncated")
        byte = buf[off]
        off += 1
        size += (byte & 0x7F) << shift
        shift += 7
        if shift > 63:
            raise ValueError("pack entry size varint is too long")
    return size, off, first


def _read_ofs_delta_base(buf, start):
    if start >= len(buf):
        raise ValueError("ofs-delta has no base offset")
    byte = buf[start]
    off = start + 1
    value = byte & 0x7F
    while byte & 0x80:
        if off >= len(buf):
            raise ValueError("ofs-delta base offset is truncated")
        byte = buf[off]
        off += 1
        value = ((value + 1) << 7) + (byte & 0x7F)
    return value, off


def _inflate_at(buf, offset, limit):
    """Inflate one zlib stream starting at offset; return (data, next_offset)."""
    d = zlib.decompressobj()
    chunk = buf[offset:limit]
    data = d.decompress(chunk)
    if not d.eof:
        raise ValueError("zlib stream at offset %d did not terminate" % offset)
    consumed = len(chunk) - len(d.unused_data)
    return data, offset + consumed


def apply_delta(base, delta):
    """Apply a git delta instruction stream to a base object's bytes."""
    off = 0

    def read_size():
        nonlocal off
        value = 0
        shift = 0
        while True:
            if off >= len(delta):
                raise ValueError("delta size varint is truncated")
            byte = delta[off]
            off += 1
            value += (byte & 0x7F) << shift
            shift += 7
            if not (byte & 0x80):
                break
            if shift > 63:
                raise ValueError("delta size varint is too long")
        return value

    source_size = read_size()
    target_size = read_size()
    if source_size != len(base):
        raise ValueError("delta expects a %d-byte base, got %d" % (source_size, len(base)))
    out = bytearray()
    while off < len(delta):
        op = delta[off]
        off += 1
        if op & 0x80:
            copy_off = 0
            copy_size = 0
            shift = 0
            for bit in range(4):
                if op & (1 << bit):
                    if off >= len(delta):
                        raise ValueError("delta copy offset is truncated")
                    copy_off += delta[off] << shift
                    off += 1
                shift += 8
            shift = 0
            for bit in range(4, 7):
                if op & (1 << bit):
                    if off >= len(delta):
                        raise ValueError("delta copy size is truncated")
                    copy_size += delta[off] << shift
                    off += 1
                shift += 8
            if copy_size == 0:
                copy_size = 0x10000
            if copy_off + copy_size > len(base):
                raise ValueError("delta copy is out of the base")
            out += base[copy_off:copy_off + copy_size]
        elif op != 0:
            lit = op & 0x7F
            if off + lit > len(delta):
                raise ValueError("delta insert is truncated")
            out += delta[off:off + lit]
            off += lit
        else:
            raise ValueError("delta opcode 0 is invalid")
    if len(out) != target_size:
        raise ValueError("delta produced %d of %d bytes" % (len(out), target_size))
    return bytes(out)


def read_pack(pack, resolve_base=None):
    """Read a packfile (v2/v3), resolving OFS/REF deltas.

    Returns {"version", "declared", "objects": {oid: (type, data)},
    "delta_count"}. resolve_base(oid) supplies a base a thin pack leaves out.
    Raises ValueError on any structural problem.
    """
    if len(pack) < 32:
        raise ValueError("pack is %d bytes, too short" % len(pack))
    if pack[:4] != b"PACK":
        raise ValueError("pack does not start with PACK")
    version = int.from_bytes(pack[4:8], "big")
    if version not in (2, 3):
        raise ValueError("unsupported pack version %d" % version)
    declared = int.from_bytes(pack[8:12], "big")
    trailer_start = len(pack) - 20
    if pack[trailer_start:] != hashlib.sha1(pack[:trailer_start]).digest():
        raise ValueError("pack trailer checksum mismatch")

    raw = []  # (offset, type_code, size, data, base_offset, base_oid)
    by_offset = {}
    off = 12
    for _ in range(declared):
        entry_off = off
        size, off, first = _read_varint_size(pack, off)
        type_code = (first >> 4) & 0x07
        base_offset = None
        base_oid = None
        if type_code == 6:
            rel, off = _read_ofs_delta_base(pack, off)
            if rel <= 0 or rel > entry_off:
                raise ValueError("ofs-delta names an invalid base offset")
            base_offset = entry_off - rel
        elif type_code == 7:
            if off + 20 > trailer_start:
                raise ValueError("ref-delta base id is truncated")
            base_oid = pack[off:off + 20].hex()
            off += 20
        elif type_code not in CODE_TYPE:
            raise ValueError("pack entry has unknown type code %d" % type_code)
        data, off = _inflate_at(pack, off, trailer_start)
        if len(data) != size:
            raise ValueError("pack entry declares %d bytes but inflated to %d" % (size, len(data)))
        entry = (entry_off, type_code, size, data, base_offset, base_oid)
        raw.append(entry)
        by_offset[entry_off] = entry
    if off != trailer_start:
        raise ValueError("pack has %d stray bytes before its trailer" % (trailer_start - off))

    objects = {}
    resolved_by_oid = {}
    resolving = set()
    delta_count = 0
    # oid -> (type, data) for entries resolved so far (REF_DELTA bases).
    oid_index = {}

    def resolve(entry):
        entry_off, type_code, _size, data, base_offset, base_oid = entry
        if entry_off in resolved_by_oid:
            return resolved_by_oid[entry_off]
        if entry_off in resolving:
            raise ValueError("delta cycle at pack offset %d" % entry_off)
        resolving.add(entry_off)
        if type_code == 6:
            base_entry = by_offset.get(base_offset)
            if base_entry is None:
                raise ValueError("ofs-delta base is not in the pack")
            btype, bdata, _boid = resolve(base_entry)
            rtype, rdata = btype, apply_delta(bdata, data)
        elif type_code == 7:
            found = oid_index.get(base_oid)
            if found is not None:
                btype, bdata = found
                rtype, rdata = btype, apply_delta(bdata, data)
            else:
                ext = resolve_base(base_oid) if resolve_base else None
                if ext is None:
                    raise ValueError("ref-delta base %s is neither in the pack nor supplied" % base_oid)
                btype, bdata = ext
                rtype, rdata = btype, apply_delta(bdata, data)
        else:
            rtype, rdata = CODE_TYPE[type_code], data
        oid = hash_object(rtype, rdata)
        resolved_by_oid[entry_off] = (rtype, rdata, oid)
        oid_index[oid] = (rtype, rdata)
        resolving.discard(entry_off)
        return rtype, rdata, oid

    for entry in raw:
        if entry[1] in (6, 7):
            delta_count += 1
        rtype, rdata, oid = resolve(entry)
        if oid not in objects:
            objects[oid] = (rtype, rdata)
    return {"version": version, "declared": declared, "objects": objects,
            "delta_count": delta_count}


def collect_reachable(roots, lookup):
    """Every object reachable from roots: commit->tree+parents, tag->target,
    tree->entries (gitlinks skipped), blob->nothing. lookup(oid) -> (type, data) or None."""
    seen = set()
    queue = [r for r in roots if is_oid(r)]
    while queue:
        oid = queue.pop()
        if oid in seen:
            continue
        obj = lookup(oid)
        if obj is None:
            raise ValueError("object %s is missing from the store" % oid)
        otype, data = obj
        seen.add(oid)
        if otype == "commit":
            c = parse_commit(data)
            queue.append(c["tree"])
            queue.extend(c["parents"])
        elif otype == "tag":
            queue.append(parse_tag(data)["object"])
        elif otype == "tree":
            for mode, _name, entry_oid in parse_tree(data):
                if mode == "160000":
                    continue
                queue.append(entry_oid)
    return seen
