# Atlas LinkedIn → Slack

A small, dependency-free Python bot that checks Atlas Motion's public, logged-out
LinkedIn company page every five minutes and sends each new JSON-LD post to Slack.

## How it behaves

- Fetches `https://www.linkedin.com/company/atlasmotion` with a browser user-agent.
- Parses `DiscussionForumPosting` objects from schema.org JSON-LD.
- Uses the numeric LinkedIn activity ID as the durable identity.
- Sends unseen posts oldest-first to the incoming webhook in `SLACK_WEBHOOK_URL`.
- Writes each successfully delivered ID to `last_seen.txt`; GitHub Actions commits it.
- Exits nonzero and leaves state untouched when the fetch or parse fails.
- If several posts are new and a later Slack send fails, already delivered IDs remain
  in the local state and are committed before the workflow reports failure.

The repository is initially seeded with all posts visible on 2026-09-03, so its
first normal run does not replay history.

## GitHub setup

1. Create a private, empty GitHub repository.
2. Push this directory to its default branch.
3. In **Settings → Secrets and variables → Actions**, create a repository secret
   named `SLACK_WEBHOOK_URL` whose value is the full Slack incoming webhook URL.
4. In **Settings → Actions → General**, allow actions and ensure workflows may have
   read/write repository permissions. The workflow also requests `contents: write`.
5. Open **Actions → Atlas LinkedIn to Slack → Run workflow**. Leave
   `send_test_alert` off for a no-op seeded run, or turn it on to send the latest
   visible post as a real test without changing state.

Scheduled workflows run only from the default branch. The schedule is offset from
the busy top of each hour. GitHub does not guarantee an exact start time, but missed
or failed fetches cannot advance the state.

## Local checks

```sh
python3 -m unittest discover -s tests -v
python3 linkedin_to_slack.py --check
```

To send a real local test (the URL stays in the environment and is never written):

```sh
SLACK_WEBHOOK_URL='https://hooks.slack.com/services/…' \
  python3 linkedin_to_slack.py --test-latest
```
