# Talking to Coding Brain

Run `codingbrain` from any folder: your home folder, a project, anywhere. It opens a conversation.
A greeting gets a greeting, a question gets an answer, and coding work starts only when you ask
for it and confirm.

```text
you> hello
Hello! I'm Coding Brain. Ask me anything about code, tell me what to build ...
you> what projects am I working on?
Projects registered with Coding Brain (2): ...
you> how should the tests in calculator be organised?
(an answer based on that project's files, README and memory, with nothing changed)
you> fix the calculator bug
Task for calculator (C:\Users\you\Projects\calculator): Fix the add bug in calculator
Run it as a coding task? ... [Y/n]
```

## How messages are understood

| You say | What happens |
| --- | --- |
| a greeting, thanks, help, goodbye | answered directly by fixed rules; no model call |
| "what projects am I working on?" | the project registry, read-only |
| a question or conversation | answered by your local model; nothing else happens |
| "explain / review / how should I … in *project*" | a read-only answer from that project's files and memory |
| "create / build a … app (website, tool, API …)" | the [new-project workflow](new-project.md), after you confirm |
| "fix / add / refactor / update …" | a coding task in the right project, after you confirm |

Fixed rules handle only the unmistakable cases. Everything else is interpreted by your local
model into an intent, the project it refers to and a one-sentence goal.

The model's interpretation is advice, not permission:
- It can only propose work, and a request without an action verb is never treated as work.
- If the model is unavailable, only unmistakable requests are acted on. Anything uncertain is
  treated as conversation.
- Nothing that changes a project happens without your "yes". That includes Git worktrees, tasks,
  commits, premium Claude/Codex budget and tools.
- An empty answer or the end of piped input never counts as consent.

**Finding the project.** Coding Brain looks for the project in this order:
1. a project named in your message;
2. the project the conversation is already about;
3. the Git project you started `codingbrain` in;
4. a project whose files match your words.

If none of these settles it, it asks "Which project do you mean?" and you answer with a number,
a name or a folder path. It never scans your home folder.

**Follow-up questions keep their context.** After a plan, "do it" runs it, after you confirm.

## Conversations are not tasks

- Chatting never creates a task, so nothing appears in `codingbrain tasks` or `/tasks`.
- Ctrl+C while the model is thinking stops the reply and starts nothing.
- A real coding task shows its live activity: planning, agents, tool calls, approvals and tests.
  It stays resumable after Ctrl+C, as before.

Conversation turns are stored in Coding Brain's data folder (`data/conversations`). Secrets are
redacted before storage. Turns about a project are tagged with it, and a conversation about one
project never receives another project's turns or memory.

## Commands inside the conversation

`/projects`  `/tasks`  `/history`  `/run <goal>`  `/new <goal>`  `/help`  `/exit`

`codingbrain run "goal"` still submits a coding task directly, and `codingbrain chat "message"`
answers one message and exits.
