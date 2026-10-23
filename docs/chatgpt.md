# Use the MOVA bridge in other chats

This package provides a **single-owner, local MCP server** over stdio. It has no HTTP listener and no account-linking website. Installing the skill or publishing its source does not connect it to ChatGPT web or mobile. Follow one of the connection routes below; neither is configured automatically.

## Prepare the local bridge

From the repository root:

```bash
python3 -m venv .venv
.venv/bin/pip install -r bridge/requirements.txt
.venv/bin/python mova-robot/scripts/mova.py login
```

Enter the MOVA region and credentials in the local terminal. The server cannot accept a login through a tool call. It uses `~/.config/mova-robot` by default. To choose another private directory, set `MOVA_CONFIG_DIR` in the environment of both login and server processes; it is never a tool argument.

Start the server through an MCP client using:

```bash
.venv/bin/python bridge/server.py
```

By default the tools are `get_capabilities`, `list_robots`, `get_status` and `list_rooms`. Add `--allow-control` to the server command to also expose `pause_robot`, `stop_robot` and `mop_room`; each mutation requires `execute=true`. These switches are execution safeguards, not user authentication. Spill-mode activation remains unsupported.

## Local Codex chats

Add this to `~/.codex/config.toml`, replacing both paths with absolute paths to this checkout:

```toml
[mcp_servers.mova_robot]
command = "/absolute/path/to/checkout/.venv/bin/python"
args = ["/absolute/path/to/checkout/bridge/server.py"]
default_tools_approval_mode = "writes"
tool_timeout_sec = 200
```

For a custom configuration directory, add:

```toml
[mcp_servers.mova_robot.env]
MOVA_CONFIG_DIR = "/absolute/path/to/private-config"
```

Restart the client, open a new chat, and ask it to call `get_capabilities`, then `list_robots`. Choose a robot from the result before requesting its status. ChatGPT web does not read this local configuration. [Official MCP configuration](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)

## Personal ChatGPT connection through Secure MCP Tunnel

Use an owner-only tunnel: **every caller reaches the same MOVA account**. Do not share this connection with other users or a shared workspace.

Create a tunnel in [Platform tunnel settings](https://platform.openai.com/settings/organization/tunnels), associated with your personal Platform organization and intended ChatGPT workspace. Creation needs Tunnels Read + Manage; operation needs Read + Use. Obtain a restricted runtime API key and install the [latest official tunnel client](https://github.com/openai/tunnel-client/releases/latest). Account and workspace policies still apply. [Official tunnel guide](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)

In Bash, enter the key without putting it in command history:

```bash
read -r -s -p "Tunnel runtime API key: " CONTROL_PLANE_API_KEY
printf '\n'
export CONTROL_PLANE_API_KEY
tunnel-client help quickstart
tunnel-client init \
  --sample sample_mcp_stdio_local \
  --profile mova-personal \
  --tunnel-id YOUR_TUNNEL_ID \
  --mcp-command "/absolute/path/to/checkout/.venv/bin/python /absolute/path/to/checkout/bridge/server.py"
tunnel-client doctor --profile mova-personal --explain
tunnel-client run --profile mova-personal
```

Keep that process and computer running. The client initiates outbound HTTPS; the bridge stays private. [Official tunnel guide](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)

In ChatGPT **Plugins → + → Add custom MCP server**, select **Tunnel**, choose that tunnel, and create the plugin. This bridge has no application OAuth; use no application authentication only within the owner-only tunnel boundary above. Test `get_capabilities`, then a status read. A successful ChatGPT tool call is the completion check. [Connection instructions](https://developers.openai.com/plugins/deploy/connect-chatgpt)

## Where the login is stored

The local client saves the MOVA session in a JSON file restricted to its OS owner (`0600` on Unix). **The file is plaintext, not encrypted by those permissions.** The bridge process needs to read it; other processes running as the same OS user may also read it. Keep it out of source control, shared folders, backups shared with others, and chat messages. The bridge does not return credentials in tool results, but robot status and room names returned by tools enter the requesting chat.

An OAuth-connected ChatGPT plugin follows a different path: ChatGPT obtains an access token and attaches it to service requests. Its server must validate the token and permissions. The public documentation does not specify ChatGPT's internal credential database or key-management implementation, so this project makes no claim about those internals. [Official authentication documentation](https://developers.openai.com/plugins/build/auth)

## A future hosted version

Multi-user hosting is **not supported** by this bridge. A hosted version would need separate OAuth authorization to the bridge, per-user account isolation, encrypted MOVA token storage with managed keys, revocation, and permission enforcement on every call. An established identity provider should supply authentication. No official public MOVA developer OAuth flow has been verified; the current login uses the community protocol. Do not expose this single-account process as an unauthenticated public endpoint.
