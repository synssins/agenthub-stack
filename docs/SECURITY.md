# Security notes

## How access works
- AgentHub has no user accounts. The index page carries a session token. Every `/api` call needs it. Requests are accepted only from `allowed_networks`, only under a host name the install is known by (an address, or a name in `allowed_origins`), and cross-origin calls are refused. The WebSocket applies the same checks.
- Whoever can load the index page from an allowed network is the owner. Keep the port off networks you do not trust.
- Registered agents, voice devices and `/v1` clients each use their own key.
- Secrets (model keys, the Home Assistant token, the camera key, bot tokens, the broker password) are stored in `data/` with mode 600 and are not sent back to the browser. The IRC server password is the exception: its settings page shows it.
- An agent with "Full access" can run any command as the service's user. The passphrase for doors and locks guards the Home Assistant tools of API agents, not a shell.
- A Discord user who is not the owner gets chat only: an API agent answers with chat tools, attaches no file and keeps no persona or name change from that turn. A CLI agent (Claude Code and the like) does not answer.
- No file under `data/` can be attached to a message, except a camera frame made for that reply and the agent's own folder.
- Traffic is plain HTTP. Put it behind a VPN or a TLS proxy before it leaves a trusted LAN.
- The speech server has no login: anyone who reaches its port can speak, add and delete voices.

## What was tested
A throwaway copy of each service, never the live one.

| Test | Tool | Scope |
|---|---|---|
| Static analysis | `bandit` | All Python of the room and the speech server |
| Dependencies | `pip-audit` | `agenthub/requirements.txt` |
| Passive scan, no credentials | OWASP ZAP baseline | Room, speech server |
| Active API scan with the session token | OWASP ZAP API scan from the OpenAPI description | Room: 132 paths, 165 operations. Speech server: all 14 routes |
| Active full scan with spider | OWASP ZAP full scan | Room |
| Every operation with no token, a wrong token and a wrong Bearer key | script | Room: 165 operations |
| Code review of the browser code for script injection | by hand | `static/` |
| Code review of the server against the OWASP Top 10 and the OWASP list for LLM applications | by hand | Routes, agent tools, Discord and IRC bridges, passphrase |
| The fixes, in a headless browser and over HTTP and WebSocket | script | Room |

## Found and fixed

| Finding | Severity | Fix |
|---|---|---|
| A message could carry HTML that ran as script in the owner's browser (an escaped backtick got raw HTML past the code guard) | High | No HTML from a message is passed through |
| An agent's hat (an SVG it writes) could carry markup that ran as script | High | Shown as a picture |
| A Discord stranger could have an agent attach any readable file, such as the stored keys, which was then posted to Discord | High | No attachment on a stranger's turn; `data/` cannot be attached at all |
| A Discord stranger could rewrite an agent's persona or name for good | High | Ignored on a stranger's turn |
| A CLI agent bound to a Discord bot gave a stranger its full tools | High | A CLI agent does not answer a stranger's turn |
| Any web page could set an agent's role from the owner's browser (`POST /api/roles/...` needed no token) | High | Needs the session token |
| No check of the host name: another site's name pointed at the server (DNS rebinding) could read the page and its token | Medium | Unknown host names are refused |
| The WebSocket checked only the token: not the network, host name or origin | Medium | Same checks as HTTP |
| `javascript:` links in messages | Medium | Links open web or site addresses only |
| A Discord display name or a decision choice could break out of a button's `onclick` | Medium | Passed as data, never as code |
| An uploaded SVG opened by its address ran as the site | Medium | Served sandboxed |
| A passphrase said after its wait ended was posted as a normal message | Low | Not posted |
| `POST /api/agents/join` could be sent by any web page | Low | Needs `Content-Type: application/json` |
| A body that is not JSON gave a 500 (92 routes) | Low | 400 |
| Scripts from CDNs without a pinned version or integrity hash | Low | Pinned, with hashes |
| No `X-Content-Type-Options` or `Referrer-Policy` | Low | Set on every response |
| Token compared in non-constant time | Low | Constant time |
| `pynacl` 1.5.0 has two known vulnerabilities | Medium | `discord.py[voice]` caps it below 1.6, so it cannot be pinned: run `pip install --no-deps "pynacl>=1.6.2"` after installing (the install guide has the step). Discord voice works with it |

## Open

| Item | Severity | Note |
|---|---|---|
| Relayed and mid-turn text from strangers | Medium | "Stranger" is judged from the newest message and the newest one naming the agent, when the turn starts. A stranger who gets agent A to address agent B, or who writes while the owner's turn is running, reaches an agent that has its tools. The sender label in the prompt is the only guard |
| The passphrase does not guard CLI agents | Medium | A Claude Code agent given the Home Assistant server calls it directly. Give house control to API agents only |
| Memory notes from a stranger's turn | Low | `remember` is a chat tool and works on those turns |
| No Content-Security-Policy | Medium | The page uses inline handlers throughout; a policy needs them moved first. It would be the second guard behind the fixes above |
| No anti-framing header | Low | Left off so the room can be embedded in a dashboard. Add `X-Frame-Options: SAMEORIGIN` at a proxy if it is not embedded |
| Open registration of remote agents is on by default | Low | Settings > Agents: switch it off, or use invites |
| The session token can be passed as `?token=` | Low | The WebSocket and media links need it; it can land in proxy logs |
| The highlight.js stylesheet has no integrity hash | Low | The theme switch swaps its address |
| `urlopen` with a configurable address (34 places) | Low | Every address comes from the owner's settings |
| Low-severity static notes (141: asserts, `subprocess`, try/except/pass) | Low | Not reviewed one by one |
| Speech server: no login, no security headers | By design | LAN only |

## Not tested
- Senses prompt building from camera descriptions, Home Assistant names and MQTT text as an injection path.
- `mcp_proxy.py`, `mcp_oauth.py`, the wrappers for CLI agents, scheduled jobs and sessions.
- How ffmpeg handles hostile Discord attachments.
- Any model server or the Strata engine as a running service.
- Load and denial-of-service behavior.
