# Security

Vittics Builder runs AI agents that read, write and execute code. Please report security problems
privately to the maintainer (open a GitHub security advisory on the repository) rather than in a
public issue.

What Vittics Builder protects:
- Agents' commands run without your API keys or tokens in their environment.
- File tools cannot leave the project workspace; a deny list blocks privilege escalation,
  deleting outside the workspace, piping downloads into a shell and `git push`.
- With `sandbox.mode: docker`, agents' commands run in a container (no network unless allowed).
- Dashboard access needs a CEO or CTO token. Keep it on localhost or behind HTTPS (e.g. Tailscale).

What it does not protect against:
- CLI engines (`cli:claude-code` and others) run on your machine under the CLI's own permission
  rules, not in the Docker sandbox.
- The deny list is a seatbelt, not a sandbox, in `sandbox.mode: local`.
- AI audits are not a substitute for a professional security review.
