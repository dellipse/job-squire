# Using Hermes Agent

Hermes Agent is an open-source AI agent harness from Nous Research. It connects to Job Squire through the MCP static API key — the same path used by scripts and other non-Claude tools.

Hermes is a good fit if you want to run a local agent against your job pipeline, prefer the Nous Hermes model family, or want to control which model handles your job search data without using Claude's infrastructure.

---

## How it connects

Hermes connects to Job Squire as an MCP client over HTTP, using a static Bearer token for authentication. You generate the token in Job Squire, put it in Hermes's config, and Hermes discovers and registers all 22 Job Squire tools automatically at startup.

This is a different path from the Claude Pro connector (which uses OAuth). The static token never expires on its own — rotate it manually in Settings → AI → MCP Connector if needed.

---

## Prerequisites

- Job Squire running with `PUBLIC_MCP_URL` set and the MCP container up
- Python 3.10 or later
- An API key for at least one AI provider (Hermes supports Nous Portal, OpenRouter, Anthropic, OpenAI, Ollama, and others — see [AI providers](https://hermes-agent.nousresearch.com/docs/integrations/providers))

---

## Step 1: Generate a static API key in Job Squire

1. Open **Settings → AI → MCP Connector**.
2. Click **Generate static API key**.
3. Copy the key immediately. It is shown once.
4. Note your `PUBLIC_MCP_URL` — you will need the full URL (e.g. `https://jobs.example.com/mcp`).

---

## Step 2: Install Hermes Agent

```bash
pip install hermes-agent
```

Run the setup wizard:

```bash
hermes install
```

The wizard prompts you to choose a provider and model. You can change this later.

Full install instructions: [hermes-agent.nousresearch.com/docs/getting-started/quickstart](https://hermes-agent.nousresearch.com/docs/getting-started/quickstart)

---

## Step 3: Configure the Job Squire MCP server

Add the following to `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  job_squire:
    url: "https://jobs.example.com/mcp"
    headers:
      Authorization: "Bearer ${JOB_SQUIRE_API_KEY}"
```

Replace `https://jobs.example.com/mcp` with your actual `PUBLIC_MCP_URL`.

To keep the key out of the config file, store it in `~/.hermes/.env`:

```
JOB_SQUIRE_API_KEY=your-key-here
```

Hermes substitutes `${JOB_SQUIRE_API_KEY}` at runtime from that file.

**Optional settings** — add these under the `job_squire` block if needed:

```yaml
    timeout: 30           # tool call timeout in seconds (default: 30)
    connect_timeout: 10   # initial connection timeout
    enabled: true
    tools:
      include:            # restrict to specific tools (omit to allow all 22)
        - get_pipeline
        - list_jobs
        - save_analysis
```

---

## Step 4: Set your AI provider and model

The active provider is set under the top-level `model:` key in `~/.hermes/config.yaml`. Example using OpenRouter:

```yaml
model: anthropic/claude-sonnet-4-5
provider:
  type: openrouter
  api_key: "${OPENROUTER_API_KEY}"
```

To switch models interactively:

```bash
hermes model
```

See [AI providers](https://hermes-agent.nousresearch.com/docs/integrations/providers) for the full list of supported providers and their config formats.

---

## Step 5: Start a session

```bash
hermes chat
```

Hermes connects to all configured MCP servers at startup, discovers the Job Squire tools, and enters a chat loop. From there you interact in natural language. Hermes selects and calls the appropriate MCP tools on its own.

To reload MCP configuration without restarting the session, type `/reload-mcp` in the chat loop.

---

## Example interactions

Once connected, you can ask things like:

- "Show me my current pipeline"
- "Which jobs are overdue for a follow-up?"
- "Score the unreviewed jobs in my Saved list"
- "Draft a follow-up email for the Acme Corp application"
- "Give me a weekly summary of what's happened in my search"

Hermes calls the appropriate Job Squire MCP tools and writes results back where applicable (scores, drafts, analysis notes).

---

## Limitations

**No per-job action buttons.** The Score, Build Kit, Interview Prep, and Draft Follow-Up buttons in the Job Squire UI only work with the Claude Pro connector. Everything with Hermes is prompt-driven through the chat loop.

**No scheduled routines.** Hermes doesn't have a built-in scheduler equivalent to Claude's Routines feature. If you want automated recurring tasks, combine Hermes with a cron job or use Job Squire's built-in Automated Features with an API provider.

**`hermes mcp serve` is stdio only.** If you need Hermes itself to act as a remote MCP server for other clients, that's not yet supported. This doesn't affect connecting Hermes to Job Squire as a client.

---

## Further reading

- [Hermes Agent documentation](https://hermes-agent.nousresearch.com/docs)
- [MCP configuration reference](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp)
- [Hermes Agent on GitHub](https://github.com/nousresearch/hermes-agent)
- [Job Squire MCP tool reference](../mcp-connector.md)
