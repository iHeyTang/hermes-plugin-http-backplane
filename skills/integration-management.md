---
name: integration-management
description: >
  Help the user add, inspect, or remove HTTP backplane integrations
  served under /integrations/<name>/* by hermes-plugin-http-backplane.
  Lifecycle is operator-driven via the `hermes integration` subcommand
  (CLI); this skill teaches the assistant how to scaffold the code and
  hand off the install command, NOT to install anything directly.
when_to_use: >
  User asks to "add", "create", "wire up", or "install" a new
  integration for an external service (lark, slack, zendesk, …) under
  /integrations/<name>/*, OR asks how the existing integrations are
  organized, OR wants to inspect / reload / remove one.
---

# Backplane integration management

Integrations live at `~/.hermes/integrations/<name>/` as small Python
packages. Each one mounts under `/integrations/<name>/*` on the local
HTTP backplane (default `127.0.0.1:9394`).

## What an integration looks like

Minimum two files:

`__init__.py`

```python
from .handler import setup  # noqa: F401
```

`handler.py`

```python
from aiohttp import web


async def handle_search(request: web.Request) -> web.Response:
    q = request.query.get("q", "")
    return web.json_response({"q": q, "results": []})


def setup(router) -> None:
    """Register routes under /integrations/<name>/<path>."""
    router.add_get("/search", handle_search)
```

Optional `integration.yaml` for metadata shown by `hermes integration list`:

```yaml
version: 0.1.0
description: short human-readable line
endpoints:
  - GET /search?q=...
```

The `router` argument supports `add_get / add_post / add_delete /
add_patch / add_put / add_route` and aiohttp-style path templates
(`/items/{id}`, `/items/{id:[0-9]+}`). Two match-info keys — `name` and
`tail` — are reserved by the dispatcher; don't use them as your own
path params.

## Workflow

1. **Help the user write the code** in the conversation. Drop the
   files into a temporary directory or just print them.
2. **Hand off the install** to the user — do NOT try to install via
   any agent tool, there isn't one. Tell them to run one of:

```bash
# Install from a directory you already have on disk
hermes integration install my-tool --from-path ./my-tool/

# Install inline (read each file with @path)
hermes integration install my-tool \
  --handler-py @handler.py \
  --yaml @integration.yaml

# Replace an existing integration of the same name
hermes integration install my-tool --from-path ./my-tool/ --overwrite
```

3. Verify with `hermes integration list`. The integration is reachable
   at `http://127.0.0.1:9394/integrations/<name>/...` as soon as the
   CLI prints `live.reloaded: true`.

## Other lifecycle commands

```bash
hermes integration list                      # show registered + failed
hermes integration reload my-tool            # pick up source edits
hermes integration remove my-tool            # delete files + unregister
```

`reload` requires a running backplane. `install` / `remove` work
offline too — file changes just apply on the next backplane start, and
the CLI says so.

## Names

`^[a-z][a-z0-9-]*$`, max 32 chars. Names that match a built-in preset
(currently `lark`) are reserved and the CLI refuses them.
