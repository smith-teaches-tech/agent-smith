# Security

## Threat model

| Threat | Mitigation |
|---|---|
| API key leaked to public | Private repo + GitHub Secrets + .gitignore + pre-commit hook |
| API key leaked to other GitHub users | GitHub Secrets are encrypted; only available to workflow |
| Runaway loop or compromised dependency exhausts budget | Hard monthly cap on Anthropic console |
| Prompt injection via malicious news headline | All external content wrapped in delimiters; Claude instructed to treat as data not instructions |
| Pages URL discovered by stranger | Not currently mitigated. Move to auth-protected hosting before adding portfolio data. |
| GitHub account compromise | Use 2FA on GitHub. Use SSH keys or fine-grained PATs. Review Actions logs periodically. |

## What private repo does NOT protect

- The `/docs` folder served by GitHub Pages is publicly readable, even from a private repo
- A leaked GitHub personal access token gives full repo access to whoever has it
- Compromised dependencies in `requirements.txt` could exfiltrate the env var during a run
- GitHub Actions logs are visible to anyone with repo access

## Pre-commit hook (recommended)

Install once:

```bash
pip install detect-secrets pre-commit
detect-secrets scan > .secrets.baseline
```

Create `.pre-commit-config.yaml`:

```yaml
repos:
  - repo: https://github.com/Yelp/detect-secrets
    rev: v1.4.0
    hooks:
      - id: detect-secrets
        args: ['--baseline', '.secrets.baseline']
```

Then:
```bash
pre-commit install
```

Now every commit is scanned for accidentally-committed secrets before it goes through.

## API key rotation

Rotate every 60–90 days:

1. Anthropic console → API keys → Create new key
2. Copy new key
3. GitHub repo → Settings → Secrets → `ANTHROPIC_API_KEY` → Update
4. Anthropic console → revoke old key
5. Trigger a manual workflow run to confirm new key works

Takes ~2 minutes. Calendar reminder recommended.

## If you suspect a key leak

1. **Immediately** revoke the key on the Anthropic console (this is the most important step — stops bleeding)
2. Check usage logs for anomalous activity
3. Generate a new key, update the GitHub Secret
4. If the leak was via a commit: `git filter-repo` to scrub history, force-push, contact Anthropic support
5. Review what other secrets/repos may share the compromise vector

## Spending limit recommendations

For this workload (3 runs/day, 22 trading days, Opus 4.7):

- **Soft alert**: $25/month
- **Hard cap**: $50/month

If you ever hit the hard cap unexpectedly, something is wrong (loop, retry storm, prompt injection causing token explosion). Investigate before raising it.
