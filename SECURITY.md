# Security Policy

## Supported Versions

Copybarista is pre-1.0. Security fixes apply to the current `main` branch.

## Reporting a Vulnerability

Report security issues privately through the repository's security advisory
channel once the public repository is created. Until then, contact the
maintainers directly.

Please include:

- A description of the issue.
- Steps to reproduce.
- Affected operating system and Python version.
- Any relevant config file or command invocation.

## Security Notes

Copybarista can delete and rewrite destination trees. It includes safety
checks for dangerous destination paths, but users should review export
configs before running them.

Copybarista shells out to `git` for Git destination exports. Commands are
executed without a shell, and arguments are passed as argv lists.
