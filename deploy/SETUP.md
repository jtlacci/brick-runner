# Set up the runner machine

The runner runs on its own Linux machine. It pulls work from GitHub; GitHub
never pushes code to it, and no GitHub workflow runs on it. Allow about one hour.

## 1. The machine

- Linux with user namespaces, for the sandbox. Ubuntu 22.04 or Debian 12 work as they are.
- **Ubuntu 24.04 and later** restrict unprivileged user namespaces through AppArmor. Allow them:

  ```sh
  echo 'kernel.apparmor_restrict_unprivileged_userns=0' | sudo tee /etc/sysctl.d/60-brick-runner.conf
  sudo sysctl --system
  ```

- Check the sandbox after you install (step 3). This must print `ok`:

  ```sh
  unshare --net --mount --map-root-user --propagation private sh -c 'mount -o remount,bind,ro / && echo ok'
  ```

- A small instance is enough to start (2 vCPU, 4 GB). Evaluations run one at a time.
- Nothing else runs on this machine, and no agent has a login on it.

## 2. A user for the runner

```sh
sudo useradd --system --create-home --home-dir /var/lib/brick-runner --shell /usr/sbin/nologin brick-runner
sudo chmod 700 /var/lib/brick-runner
```

## 3. Install

```sh
sudo apt-get install -y git python3-venv
sudo mkdir -p /opt/brick-runner && sudo chown brick-runner /opt/brick-runner
sudo -u brick-runner python3 -m venv /opt/brick-runner/venv
# brick-runner is a private repository: give pip a token with Contents: Read on it.
sudo -u brick-runner /opt/brick-runner/venv/bin/pip install \
  "git+https://x-access-token:${GITHUB_TOKEN}@github.com/jtlacci/brick-runner@main"
```

Install it as above, not as an editable checkout: the sandbox makes the
installed package read-only, and an editable checkout is one more place to protect. Install your
brick repository's own third-party dependencies into the same environment.

## 4. GitHub access

Create a **fine-grained personal access token** (or a GitHub App) for the brick
repository only, with:

| Permission | Access |
| --- | --- |
| Contents | Read |
| Pull requests | Read |
| Commit statuses | Read and write |
| Issues | Read and write (pull-request comments are issue comments) |

Then, in the brick repository's settings:

1. **Branch protection on `main`:** require the `brick-runner` status check, require a pull request, and require review from code owners.
2. **CODEOWNERS:** copy `.github/CODEOWNERS.example` from brick-framework to `.github/CODEOWNERS`.
3. **Agents use their own GitHub identity** (for example the Claude GitHub App), never your login. The runner accepts `/brick holdout` and `/brick promote` only from the logins in `--allow`, and never from bots.

## 5. S3 backup

1. Create a bucket. Turn on **versioning**, and block all public access.
2. Create an IAM user or role with `deploy/iam-policy.json` (replace `YOUR-BUCKET`). The policy allows reads and writes but no deletes.
3. Create an **empty private GitHub repository** for the store's pointer files, for example `jtlacci/brick-evidence`. Give the runner's token **Contents: Read and write** on it, or use a deploy key.

## 6. Configure

```sh
sudo install -d -m 700 -o brick-runner /etc/brick-runner
sudo -u brick-runner tee /etc/brick-runner/env >/dev/null <<'EOF'
BRICK_RUNNER_GITHUB_TOKEN=github_pat_...
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_DEFAULT_REGION=us-east-1
EOF
sudo chmod 600 /etc/brick-runner/env

sudo -u brick-runner env BRICK_RUNNER_HOME=/var/lib/brick-runner \
  /opt/brick-runner/venv/bin/brick-runner remote \
  --url s3://YOUR-BUCKET/brick-runner --region us-east-1 \
  --git https://github.com/jtlacci/brick-evidence.git
```

The workers never see these variables: each sandbox starts with a clean environment.

**The signing key.** The first evaluation creates `/var/lib/brick-runner/runner.key`.
Copy it to a password manager or AWS Secrets Manager. It is never backed up
to S3, and without it a restored runner cannot verify old holdout results.

## 7. Start

```sh
sudo cp deploy/brick-runner.service /etc/systemd/system/   # edit --github and --allow first
sudo systemctl daemon-reload
sudo systemctl enable --now brick-runner
journalctl -u brick-runner -f
```

Open a pull request that changes one brick's `src/`. Within a minute you see a
pending `brick-runner` status, then the result and one comment, updated on each push.

## Daily use

| You want to | Do this |
| --- | --- |
| Evaluate a code change | Open or push to a pull request into `main` |
| Change an evaluation | A pull request that changes only `evaluation/evaluate.py`; the comment shows the defect check |
| Run the holdout | Comment `/brick holdout <brick>` on any pull request |
| Allow a second holdout run | Comment `/brick holdout <brick> rerun: <your reason>` |
| Promote | Comment `/brick promote <brick>` |
| Pin real data | On the machine: `brick-runner ingest --spool DIR`, then `brick-runner dataset --brick B`; put the hash in `data.lock` in a pull request |
| See every trial | `mlflow ui --backend-store-uri sqlite:////var/lib/brick-runner/mlflow.db` through an SSH tunnel |

## Restore on a new machine

Repeat steps 1 to 4 and 6 (not `remote`), copy `runner.key` back, then:

```sh
sudo -u brick-runner env BRICK_RUNNER_HOME=/var/lib/brick-runner \
  /opt/brick-runner/venv/bin/brick-runner restore --git https://github.com/jtlacci/brick-evidence.git
```

Restore into the same home path as before (`/var/lib/brick-runner`); MLflow
records absolute paths.

## What is not covered yet

- **Production capture.** Production machines write spool files (`BRICK_CAPTURE_DIR`). Moving them to the runner machine for `ingest` is not automated yet.
- **Alerts.** Watch the service with your usual monitoring, for example on `cycle-error` and `sync-error` log events.
