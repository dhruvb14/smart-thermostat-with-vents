# Contributing to Plenum

Thanks for helping out. Bug reports, fixes, and features are all welcome.

## Before you open a pull request

- **Read the developer notes.** [`CLAUDE.md`](./CLAUDE.md) is the project's
  rulebook: the °F storage contract, the time-zone contract, the test and
  coverage gates, and the common pitfalls. Most CI failures on a first PR are
  covered there.
- **Run the checks locally.** From `smart_vent/`:
  `ruff check backend/ && ruff format --check backend/ && python -m pytest backend/tests/`.
  From `smart_vent/frontend/`: `npm run lint && npm run format:check && npx vitest run`.
- **Add the license header to new files.** Every source file starts with
  `SPDX-License-Identifier: AGPL-3.0-or-later` in a comment (line 2 after a
  shebang or `<!doctype html>`). CI fails without it.
- **Never paste real names from a live install.** Room names are often
  people's names. Replace them with invented ones in issues, PRs, tests, and
  screenshots.

## Contributor License Agreement (CLA)

Plenum is licensed to everyone under the
[GNU AGPL v3.0 or later](./LICENSE). The maintainer also sells separate
commercial licenses to companies that want to use Plenum in a closed-source
product (see the README's "Commercial licensing" section). To offer both, the
maintainer needs your permission to include your code in those commercial
licenses as well as in the AGPL release.

That permission is the [Plenum Contributor License Agreement](./CLA.md). In
short:

- **You keep your copyright.** The CLA grants a license; it does not transfer
  ownership.
- **You let the maintainer relicense your contribution**, including under
  commercial terms, and you grant a patent license for it.
- **Your contribution stays open source.** Everything accepted into the public
  repository remains available under the AGPL.
- **You confirm the code is yours to give**, and that your employer agrees if
  they own what you write.

### How to sign

You sign once, on your first pull request:

1. Open your pull request. The **CLA Assistant** bot comments with a link to
   the CLA.
2. Read it, then reply on the pull request with exactly:

   ```
   I have read the CLA Document and I hereby sign the CLA
   ```

3. The `CLA` check turns green. Your signature is recorded and covers all your
   later pull requests.

If the check stays red after you sign, comment `recheck`. Every commit in the
pull request must be linked to a GitHub account that has signed, so make sure
your git commit email is one attached to your GitHub account.

Pull requests the maintainer opens from branches in this repository, and
automated ones such as Dependabot, do not need a signature.
