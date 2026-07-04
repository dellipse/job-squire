# Using OpenClaw

OpenClaw is a self-hosted gateway that connects messaging apps to AI agents. Once configured, you can interact with your job pipeline from Telegram, WhatsApp, iMessage, Discord, or any other channel OpenClaw supports — without opening a browser.

This is a good fit if you want to check on follow-ups or ask for a pipeline summary from your phone, using whatever chat app you already have open.

OpenClaw is MIT licensed and runs on your own hardware or a server. Source: [github.com/openclaw/openclaw](https://github.com/openclaw/openclaw)

---

## How it connects

OpenClaw connects to Job Squire as an MCP client, using the static Bearer token for authentication. You generate the token in Job Squire, add Job Squire as a tool in OpenClaw's config, and OpenClaw makes it available to whatever AI agent and chat channel you have set up.

The agent model is configured separately in OpenClaw — you can point it at any provider you prefer (Anthropic, OpenRouter, Ollama, etc.).

---

## Prerequisites

- Job Squire running with `PUBLIC_MCP_URL` set and the MCP container up
- Node.js 24 (recommended) or Node 22 LTS (`22.19+`)
- An API key for at least one AI provider configured in OpenClaw

---

## Step 1: Generate a static API key in Job Squire

1. Open **Settings → AI → MCP Connector**.
2. Click **Generate static API key**.
3. Copy the key immediately. It is shown once.
4. Note your `PUBLIC_MCP_URL` (e.g. `https://jobs.example.com/mcp`).

The static token and Claude Pro OAuth can coexist — generating a static key does not affect any existing Claude Pro connection.

---

## Step 2: Install OpenClaw

```bash
npm install -g openclaw@latest
```

Run onboarding:

```bash
openclaw onboard --install-daemon
```

The onboarding wizard walks you through provider setup and installs OpenClaw as a background service. Full instructions: [docs.openclaw.ai/start/getting-started](https://docs.openclaw.ai/start/getting-started)

---

## Step 3: Add Job Squire as an MCP tool

OpenClaw's config lives at `~/.openclaw/openclaw.json`. Add a `mcp` entry pointing at your Job Squire MCP server:

```json
{
  "mcp": {
    "servers": {
      "job_squire": {
        "url": "https://jobs.example.com/mcp",
        "headers": {
          "Authorization": "Bearer your-static-key-here"
        }
      }
    }
  }
}
```

Replace `https://jobs.example.com/mcp` with your `PUBLIC_MCP_URL` and the key with the one you copied in Step 1.

After saving the config, restart the Gateway or reload config:

```bash
openclaw restart
```

OpenClaw will discover the Job Squire tools automatically at startup.

> For the full MCP server configuration options, see [docs.openclaw.ai/tools](https://docs.openclaw.ai/tools).

---

## Step 4: Connect a messaging channel

Telegram is the fastest channel to set up. Full channel documentation: [docs.openclaw.ai/channels](https://docs.openclaw.ai/channels)

**Telegram:**

1. Open Telegram and message [@BotFather](https://t.me/BotFather).
2. Send `/newbot` and follow the prompts to create a bot. Copy the token BotFather gives you.
3. Add the token to your OpenClaw config:

```json
{
  "channels": {
    "telegram": {
      "token": "your-telegram-bot-token",
      "allowFrom": ["your-telegram-user-id"]
    }
  }
}
```

4. Restart OpenClaw and message your bot in Telegram.

The `allowFrom` list restricts which Telegram user IDs can send commands. Find your user ID by messaging [@userinfobot](https://t.me/userinfobot) in Telegram.

Other supported channels (WhatsApp, iMessage, Discord, Slack, Signal, and more) follow the same pattern — add them to `channels` in the config. See the channel-specific guides at [docs.openclaw.ai/channels](https://docs.openclaw.ai/channels) for each one.

---

## Step 5: Open the dashboard (optional)

```bash
openclaw dashboard
```

This opens the browser-based Control UI at `http://127.0.0.1:18789/` for chat, config, session management, and viewing what tools are registered.

---

## Example interactions

Once connected, you can message your bot from any linked channel:

- "What's overdue in my job search?"
- "Give me a morning briefing"
- "Score the unreviewed jobs in my Saved list"
- "Draft a follow-up for my application at Acme Corp"
- "Who have I not heard back from in the last two weeks?"

The OpenClaw agent calls the appropriate Job Squire MCP tools and replies with the result. The agent model you configured in OpenClaw handles the reasoning; Job Squire's tools handle reading and writing the data.

---

## Limitations

**No per-job action buttons.** The Score, Build Kit, Interview Prep, and Draft Follow-Up buttons in the Job Squire UI only appear when the Claude Pro connector is active. Everything with OpenClaw is conversational.

**Agent capability depends on your configured model.** A weaker model may not correctly chain MCP tool calls for complex requests like building a full application kit. A larger model (via OpenRouter, Anthropic, or a capable local model) works better for analysis tasks.

**OpenClaw as an MCP server.** If you want OpenClaw itself to be an MCP server for other clients, check the current docs — support for this varies by version.

---

## Further reading

- [OpenClaw documentation](https://docs.openclaw.ai)
- [Channel setup guides](https://docs.openclaw.ai/channels)
- [OpenClaw on GitHub](https://github.com/openclaw/openclaw)
- [Job Squire MCP tool reference](../mcp-connector.md)
