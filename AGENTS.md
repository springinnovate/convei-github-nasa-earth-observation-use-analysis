# Repository conventions

## Python names and docstrings

- Use descriptive names that state the action and the data or service involved,
  such as `fetch_first_cmr_collection` or `read_first_content_match`.
- Use Google-style docstrings on every Python function. Start with what the
  function does for its caller. For substantial functions, explain when to call
  it, the important transformations or side effects, and the returned result.
- Describe arguments, return values or yielded values, and expected exceptions
  in the appropriate Google-style sections.
- Explain domain abbreviations at first use. Put connection and implementation
  details where they help explain an argument or behavior.
- Describe command entry points in terms of the command's inputs, work, output,
  and exit status. The conventional name `main` is appropriate for entry points.
- Keep names and documentation consistent across callers and tests.

## User documentation

- Write the README for someone installing and running the current commands.
  Include working examples, output files, options, and status interpretation.
- Use direct descriptions of current behavior. Omit development plans, future
  features, repeated research goals, and comparisons with things the software
  does not do.
- Explain search scope and completion statuses where users need them to
  interpret their results. Keep wording concrete and concise.

## Git branches

- New features: `feature/{issue-number}-{short-description}`.
- Fixes: `bugfix/{issue-number}-{short-description}`.
- Maintenance, documentation, infrastructure, and refactors:
  `task/{issue-number}-{short-description}`.
- Use lowercase kebab-case descriptions. Ask for an issue number if none exists.
- Do not use `codex` in branch names; use `richpsharp` when a username is needed.

## Pull request reviews

- Commit changes that address review comments.
- Reply to each comment with an explanation and a clickable link to the commit.
- Leave review threads unresolved so the user can resolve them.
