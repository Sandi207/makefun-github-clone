[![make.fun · github-core-clone-01](https://make.fun/badge/github-core-clone-01.svg)](https://make.fun/bounty/github-core-clone-01?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)
[![rank](https://make.fun/badge/github-core-clone-01/Sandi207/makefun-github-clone/rank.svg)](https://make.fun/bounty/github-core-clone-01/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)
[![reward](https://make.fun/badge/github-core-clone-01/Sandi207/makefun-github-clone/reward.svg)](https://make.fun/bounty/github-core-clone-01?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)

# makefun-github-clone

A standalone GitHub-compatible git forge in Python 3 stdlib only, built for [make.fun's github-core-clone-01 bounty](https://make.fun/bounty/github-core-clone-01). One process, its own store, no git binary, no UI, no outside service.

It implements git smart HTTP (upload-pack protocol v2 + receive-pack v0 with report-status, side-band-64k and delete-refs, fast-forward only), REST v3 (repos, contents, refs, labels, issues, pulls, squash merge), GraphQL (issues, pull requests, repository, mutations) and MCP at `/mcp` (JSON-RPC 2.0, Streamable HTTP). The server runs on a single asyncio event loop, so 50 concurrent clones stay fast.

Local practice harness: **120/120 conformance, 986–990/1000 total** (perf 186–190: push 50, clone 41–43, read 49–50, write 45–48).

## Run it

```sh
python3 main.py
```

There is nothing to install: the program uses the Python 3.13 standard library only, and the trial has no package manager and no internet.

## The lifecycle (main.py)

The env is a server the harness starts beside yours. Its origin is in `MAKEFUN_ENV_URL`
(`http://store:8080` in a trial).

1. **Bind an ephemeral port.** The server binds port 0 and lets the kernel pick one. Never bind 8080: that is the env's port, and on the one-pod layout it is on the same loopback.
2. **Register.** `POST $MAKEFUN_ENV_URL/register` with `{"port": <the port you bound>}`, as JSON. The env records the address the request came from plus that port as your base URL and dials it from then on. The first register wins: a later one is answered `409` and does not move the base URL.
3. **Serve until done.** The harness polls `GET $MAKEFUN_ENV_URL/done`; when it answers done, the entry exits 0.

## Layout

- `main.py` — entrypoint: binds the port, registers with the env, drives the asyncio loop.
- `server.py` — asyncio HTTP/1.1 server (keep-alive, chunked bodies, `Expect: 100-continue`).
- `gitobj.py` — git objects (SHA-1), trees/commits/tags, pkt-lines, packfile read/write (v2/v3, OFS/REF deltas).
- `gitproto.py` — smart HTTP: v2/v0 advertisements, ls-refs, fetch, receive-pack.
- `rest.py` — REST v3 endpoints.
- `graphql.py` — hand-written GraphQL parser and executor.
- `mcp.py` — MCP JSON-RPC 2.0 endpoint.
- `store.py` — in-memory store: repos, issues, PRs, labels, comments, squash merge (logical clock, no wall clock — replay-identical).

## Referral

If you want to try make.fun yourself: https://make.fun/r/fx69qdn1
