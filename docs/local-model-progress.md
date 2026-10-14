# Local model progress and bounded edits

Local coding requests use streamed Ollama responses. Inline activity reports observed output chunks, character counts and output rate without publishing generated reasoning or unfinished code. A heartbeat only reports that a request remains open; it does not certify useful progress. Final responses report the token counts supplied by Ollama.

The default limits are 300 seconds for first output, 60 seconds without further output, and 1,800 seconds for the entire request. Empty stream frames do not extend these limits. Cancellation closes the current request without stopping the shared model service. A failed or truncated response is not marked successful, and a requirement-generation timeout blocks the task instead of silently skipping its checks.

The default context is 8,192 tokens with at most 2,048 output tokens. Repository tools provide bounded file ranges and compact workflow evidence. Large context is explicitly excerpted while retaining the user goal. Existing-file proposals can use inclusive line ranges or an exact, unique old-text match. Edits are materialized into the normal full diff before syntax checks, sandbox tests and approval; ambiguous matches, overlapping ranges and mixed replacement/edit instructions are rejected.

Validation on the development PC included a real CPU-only qwen2.5-coder:7b request against a source file over 1,200 lines. The bounded change passed the existing test and eight model-generated requirement checks in the offline Docker sandbox. This verifies the exercised path; model speed and success on other goals still depend on hardware and the requested work.
