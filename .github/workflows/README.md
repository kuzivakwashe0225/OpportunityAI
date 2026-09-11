# Automated deploys

`deploy.yml` runs the test suite on every push to `master` and, only if it
passes, deploys to the production server over SSH and then verifies the API
actually came back up.

## One-time setup: three repository secrets

Settings → Secrets and variables → Actions → **New repository secret**:

| Secret | Value |
|---|---|
| `DEPLOY_HOST` | `161.97.176.218` |
| `DEPLOY_USER` | `isaiah` |
| `DEPLOY_SSH_KEY` | the **private** key whose public half is already in the server's `~/.ssh/authorized_keys` |

For `DEPLOY_SSH_KEY`, paste the whole private key file including the
`-----BEGIN...-----` and `-----END...-----` lines. The key at
`~/.ssh/opportunityai_deploy` on the owner's machine is already authorised on
the server (verified), so its private half is the one to paste.

**Never commit the private key.** It goes in the secret and nowhere else -
that is the entire reason the workflow reads it from `secrets` rather than
from a file in the repo.

## Why it is shaped this way

- **Tests gate the deploy.** `deploy` has `needs: test`, so a red suite stops
  the push from reaching production. Automating deploys is only an
  improvement if it makes shipping a broken commit harder, not faster.
- **Deploys queue rather than race.** `concurrency` with
  `cancel-in-progress: false` - two deploys running at once leave the server
  in whichever order they happen to finish, which is not necessarily the
  newest commit.
- **The host key is pinned on first use**, not disabled. A swapped host key
  fails the deploy instead of being accepted silently.
- **searxng is restarted explicitly.** Its config is a bind-mounted file, so
  `docker compose up -d` does not recreate it when only `settings.yml`
  changed - an engine change would otherwise deploy to disk and nowhere else.
  This was a real miss during a manual deploy before this workflow existed.
- **The health check is part of the deploy.** Without it the job reports
  success whenever the SSH command exits 0, including when the API is
  crash-looping behind it.

## Manual deploys still work

Nothing here replaces the documented manual path in `AGENTS.md`
(`git pull && docker compose up -d --build`); the workflow just does the same
thing on your behalf, with the tests and health check attached.
