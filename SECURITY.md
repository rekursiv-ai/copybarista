# Security Policy

## Supported Versions

Copybarista is pre-1.0. Security fixes apply to the current `main` branch.

## Reporting a Vulnerability

Report security issues privately through GitHub Security Advisories for this
repository. If advisories are unavailable, contact the maintainers directly
instead of opening a public issue.

Please include:

- A description of the issue.
- Steps to reproduce.
- Affected operating system and Python version.
- Any relevant config file or command invocation.

## Security Notes

Copybarista can delete and rewrite destination trees. It includes safety
checks for dangerous destination paths, but users should review export
configs before running them.

For private-to-public exports, treat the config and release-tree policy as part
of the privacy boundary. Export only explicit source paths, strip or rewrite
private text through checked-in transforms, and run release-tree checks in the
public repository before packaging or publishing.

For public-to-source imports, keep write credentials out of validation steps
that execute imported public changes. Token-bearing PR creation should run
trusted workflow code captured before import, or plain `git` and `gh` commands.

Interrupted folder exports can leave the destination tree partially rewritten.
If that happens, inspect or clean the destination and rerun the export with
`--force`.

Copybarista shells out to `git` for Git destination exports. Commands are
executed without a shell, and arguments are passed as argv lists.
