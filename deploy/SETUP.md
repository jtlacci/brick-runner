# Set up the runner machine

On AWS, `deploy/aws/` does all of this: see [deploy/aws/README.md](aws/README.md).
This guide is for any other Linux machine.

The runner runs on its own Linux machine. It pulls work from GitHub; GitHub
never pushes code to it, and no GitHub workflow runs on it. Allow about one hour.

## 1. The machine

- Linux with user namespaces, for the sandbox. Ubuntu 22.04 or Debian 12 work as they are.
- **Ubuntu 24.04 and later** restrict unprivileged user namespaces through AppArmor. Allow them:

  ```sh
  echo 'kernel.apparmor_restrict_unprivileged_userns=0' | sudo tee /etc/sysctl.d/60-brick-runner.conf
  sudo sysctl --system
  ```

  (An AppArmor profile that grants `userns` to the runner's Python only is narrower, if you prefer.)

- Check the sandbox after you install (step 3). This must print `ok`:

  ```sh
  sudo -u brick-runner unshare --net --mount --map-root-user --propagation private \
    sh -c 'mount -o remount,bind,ro / && echo ok'
  ```

- A small instance is enough to start (2 vCPU, 4 GB). Evaluations run one at a time.
- Nothing else runs on this machine, and no agent has a login on it.

## 2. A user for the runner

```sh
sudo useradd --system --create-home --home-dir /var/lib/brick-runner --shell /usr/sbin/nologin brick-runner
sudo chmod 700 /var/lib/brick-runner
```

## 3. Install, owned by root

```sh
sudo apt-get install -y git python3-venv openssl
sudo python3 -m venv /opt/brick-runner/venv
sudo /opt/brick-runner/venv/bin/pip install "git+https://github.com/jtlacci/brick-runner.git@<commit>"
```

Root owns `/opt/brick-runner`, so neither the runner user nor anything it
runs can change the runner's code. Do not use an editable checkout. Install
your brick repository's own third-party dependencies into the same environment.

## 4. A GitHub App for the runner

The runner needs its own GitHub identity, so that only it can set the
`brick-runner` status.

1. **Create a GitHub App** (Settings → Developer settings → GitHub Apps → New). No webhook. Repository permissions:

   | Permission | Access |
   | --- | --- |
   | Contents | Read |
   | Pull requests | Read |
   | Commit statuses | Read and write |
   | Issues | Read and write (pull-request comments are issue comments) |

2. **Generate a private key** and note the App ID.
3. **Install the App on the brick repository only.** Note the installation ID (it is in the installation's URL).
4. **Branch protection on `main`:**
   - require a pull request, and review from code owners;
   - require the `brick-runner` status check, and **set its expected source to this App**, so no other identity can set it;
   - require branches to be up to date before merging.
5. **CODEOWNERS:** copy `.github/CODEOWNERS.example` from brick-framework to `.github/CODEOWNERS`.
6. **Agents** use their own GitHub identity (for example the Claude GitHub App), with **no** Commit statuses permission, and are never in `--allow`. The runner accepts `/brick holdout` and `/brick promote` only from the logins in `--allow`, never from bots, and never from a comment that was edited.

A fine-grained personal access token also works for a first trial
(`BRICK_RUNNER_GITHUB_TOKEN`), but its statuses cannot be pinned to a source.
Move to the App before you rely on the check.

## 5. S3 backup

1. Create a bucket. Turn on **versioning** (or Object Lock), and block all public access.
2. Create an IAM user or role with `deploy/iam-policy.json` (replace `YOUR-BUCKET`). The policy allows reads and writes but no deletes. If the bucket uses SSE-KMS, add `kms:Encrypt`, `kms:Decrypt` and `kms:GenerateDataKey` for its key.
3. Create an **empty private GitHub repository** for the store's pointer files, for example `jtlacci/brick-evidence`. Give the runner a **deploy key** with write access to that repository only, and protect its `main` branch from force pushes:

   ```sh
   sudo -u brick-runner ssh-keygen -t ed25519 -N '' -f /var/lib/brick-runner/.ssh/id_ed25519
   sudo cat /var/lib/brick-runner/.ssh/id_ed25519.pub   # add as a deploy key, "Allow write access"
   sudo -u brick-runner ssh -o StrictHostKeyChecking=accept-new -T git@github.com || true
   ```

## 6. Configure

Root owns the secrets. systemd reads the environment file as root before it
starts the service, and every sandbox worker sees `/etc/brick-runner` as an
empty folder.

```sh
sudo install -d -m 755 -o root /etc/brick-runner
sudo install -m 600 -o root ./your-app.private-key.pem /etc/brick-runner/app-key.pem
sudo tee /etc/brick-runner/env >/dev/null <<'EOF'
BRICK_RUNNER_APP_ID=123456
BRICK_RUNNER_INSTALLATION_ID=7890123
BRICK_RUNNER_APP_KEY=/run/credentials/brick-runner.service/app-key
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_DEFAULT_REGION=us-east-1
EOF
sudo chmod 600 /etc/brick-runner/env
```

The unit passes the key to the service with `LoadCredential=`, so the
service reads it from `/run/credentials/...` and the file itself stays root-only.
The sandbox hides `/run/credentials` too.

Set the backup remotes (once):

```sh
sudo -u brick-runner env BRICK_RUNNER_HOME=/var/lib/brick-runner \
  /opt/brick-runner/venv/bin/brick-runner remote \
  --url s3://YOUR-BUCKET/brick-runner --region us-east-1 \
  --git git@github.com:jtlacci/brick-evidence.git
```

This step needs the AWS variables only for later pushes; it does not contact S3.

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
pending `brick-runner` status, then the result and one comment, updated on each
push. If the log says `backup-not-configured`, step 6's `remote` command did not run.

## Daily use

| You want to | Do this |
| --- | --- |
| Evaluate a code change | Open or push to a pull request into `main` |
| Change an evaluation | A pull request that changes only `evaluation/evaluate.py`; the comment shows the defect check |
| Run the holdout | Comment `/brick holdout <brick>` on any pull request (a new comment; edited comments never run) |
| Allow a second holdout run | Comment `/brick holdout <brick> rerun: <your reason>` |
| Promote | Comment `/brick promote <brick>` |
| Pin real data | On the machine: `brick-runner ingest --spool DIR`, then `brick-runner dataset --brick B`; put the hash in `data.lock` in a pull request |
| See every trial | `mlflow ui --backend-store-uri sqlite:////var/lib/brick-runner/mlflow.db` through an SSH tunnel |

When `main` moves, the runner evaluates every open pull request again against
the new `main`. Unchanged parts are cached, so this is cheap.

## Restore on a new machine

Repeat steps 1 to 4 and 6 (not `remote`), copy `runner.key` back, then:

```sh
sudo -u brick-runner env BRICK_RUNNER_HOME=/var/lib/brick-runner \
  /opt/brick-runner/venv/bin/brick-runner restore --git git@github.com:jtlacci/brick-evidence.git
```

Restore into the same home path as before (`/var/lib/brick-runner`); MLflow
records absolute paths.

## What is not covered yet

- **Production capture.** Production machines write spool files (`BRICK_CAPTURE_DIR`). Moving them to the runner machine for `ingest` is not automated yet.
- **Review comments.** Only ordinary pull-request comments run `/brick` commands, not comments on a diff line.
- **Alerts.** Watch the service with your usual monitoring, for example on `cycle-error`, `github-error` and `sync-error` log events.
