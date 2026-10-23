# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

import os

from fastmcp import FastMCP

mcp = FastMCP("local")


@mcp.tool(annotations={"readOnlyHint": True})
def ping() -> str:
    return "pong:" + os.environ.get("LOCAL_TOKEN", "unset")


@mcp.tool(annotations={"readOnlyHint": True})
def leaked() -> str:
    return "keys:" + ",".join(sorted(k for k in os.environ if k.startswith("PARENT_SECRET")))


if __name__ == "__main__":
    mcp.run()
