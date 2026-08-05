# Security Policy

## Why this file exists

copybarista can delete and rewrite destination trees, and it is the boundary
that keeps private source out of a public repository. A config or transform bug
can publish text meant to stay private, or overwrite a tree not meant to be
touched. Security reports need a private path so exploit details are not
published before review.

## Reporting a vulnerability

Please report suspected security vulnerabilities privately by emailing hello@rekursiv.ai.

Include:

- Affected version or commit.
- Steps to reproduce.
- Expected impact.
- Any suggested mitigation.

Please do not open public issues for vulnerabilities until we have investigated and coordinated disclosure.

## Scope

Security reports are especially useful for:

- Private-to-public export leaks: source paths, transforms, or leak checks that
  let private text reach the public tree.
- Destination handling that deletes or rewrites a tree outside the configured
  destination.
- Credential exposure in public-to-source imports, especially write tokens
  reachable from validation steps that execute imported public changes.
- Argument or command injection in the `git` and `gh` invocations.
- Dependency or packaging issues that affect installed users.
- Supply-chain concerns in the published wheel or its dependency set.

## Security notes

copybarista can delete and rewrite destination trees. It includes safety
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

copybarista shells out to `git` for Git destination exports. Commands are
executed without a shell, and arguments are passed as argv lists.
