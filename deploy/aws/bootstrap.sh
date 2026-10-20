#!/bin/bash
# brick-runner machine bootstrap, run once by cloud-init as root (EC2 user data).
#
# Secrets never leave this machine except into SSM Parameter Store:
#   - the deploy key for the evidence repository is generated here; only its
#     public half is published (SSM), for a human or an agent to add on GitHub;
#   - the GitHub App key is fetched here from GitHub's manifest conversion, with
#     a one-time code placed in SSM; it is then kept root-only and in SSM;
#   - the runner's signing key is generated here and kept in SSM for restores.
# AWS access comes from the instance role. There are no AWS keys anywhere.
#
# Values filled in at launch:
#   REGION, BUCKET, PREFIX, BRICK_REPO, EVIDENCE_REPO, ALLOW, RUNNER_REF, PARAMS
set -euo pipefail
exec > >(tee -a /var/log/brick-runner-bootstrap.log) 2>&1

REGION="@@REGION@@"
BUCKET="@@BUCKET@@"
PREFIX="brick-runner"
BRICK_REPO="@@BRICK_REPO@@"          # owner/name the service watches
EVIDENCE_REPO="@@EVIDENCE_REPO@@"    # owner/name for the store's pointer files
ALLOW="@@ALLOW@@"                    # GitHub logins allowed to /brick holdout and /brick promote
RUNNER_REF="@@RUNNER_REF@@"          # the brick-runner commit to install
PARAMS="/brick-runner"
HOME_DIR=/var/lib/brick-runner
VENV=/opt/brick-runner/venv

say() { echo "[bootstrap $(date -u +%FT%TZ)] $*"; }
ssm_get() { aws ssm get-parameter --region "$REGION" --name "$PARAMS/$1" --with-decryption --query Parameter.Value --output text 2>/dev/null || true; }
ssm_put() { aws ssm put-parameter --region "$REGION" --name "$PARAMS/$1" --type "$2" --value "$3" --overwrite >/dev/null; }
status() { ssm_put status String "$1"; say "status: $1"; }

# ---------------------------------------------------------------- packages
say "packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q git python3-venv openssl unzip curl
if ! command -v aws >/dev/null; then
  curl -sSL "https://awscli.amazonaws.com/awscli-exe-linux-$(uname -m).zip" -o /tmp/awscli.zip
  unzip -q /tmp/awscli.zip -d /tmp && /tmp/aws/install && rm -rf /tmp/aws /tmp/awscli.zip
fi
status "installing"

# ---------------------------------------------------------------- sandbox support
echo 'kernel.apparmor_restrict_unprivileged_userns=0' > /etc/sysctl.d/60-brick-runner.conf
sysctl --system >/dev/null

# ---------------------------------------------------------------- user
id brick-runner >/dev/null 2>&1 || useradd --system --create-home --home-dir "$HOME_DIR" --shell /usr/sbin/nologin brick-runner
chmod 700 "$HOME_DIR"
install -d -m 700 -o brick-runner -g brick-runner "$HOME_DIR/.ssh"
sudo -u brick-runner unshare --net --mount --map-root-user --propagation private \
  sh -c 'mount -o remount,bind,ro / && echo sandbox-ok' | grep -q sandbox-ok || { status "error: user namespaces are not available"; exit 1; }

# ---------------------------------------------------------------- install, owned by root
say "installing brick-runner at $RUNNER_REF"
python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q "git+https://github.com/jtlacci/brick-runner.git@$RUNNER_REF"
chown -R root:root /opt/brick-runner

# ---------------------------------------------------------------- the runner's signing key
KEY_B64="$(ssm_get runner-key)"
if [ -z "$KEY_B64" ] || [ "$KEY_B64" = "None" ]; then
  KEY_B64="$(head -c 32 /dev/urandom | base64 -w0)"
  ssm_put runner-key SecureString "$KEY_B64"
  say "created a new signing key (kept in SSM)"
fi
echo "$KEY_B64" | base64 -d > "$HOME_DIR/runner.key"
chown brick-runner:brick-runner "$HOME_DIR/runner.key" && chmod 600 "$HOME_DIR/runner.key"

# ---------------------------------------------------------------- the evidence deploy key
if [ ! -f "$HOME_DIR/.ssh/evidence" ]; then
  sudo -u brick-runner ssh-keygen -q -t ed25519 -N '' -C "brick-runner evidence" -f "$HOME_DIR/.ssh/evidence"
fi
ssm_put pub/evidence String "$(cat "$HOME_DIR/.ssh/evidence.pub")"
cat > "$HOME_DIR/.ssh/config" <<EOF
Host github-evidence
  HostName github.com
  User git
  IdentityFile $HOME_DIR/.ssh/evidence
  IdentitiesOnly yes
  StrictHostKeyChecking accept-new
EOF
chown brick-runner:brick-runner "$HOME_DIR/.ssh/config" && chmod 600 "$HOME_DIR/.ssh/config"

# ---------------------------------------------------------------- the GitHub App
install -d -m 755 -o root -g root /etc/brick-runner
APP_KEY="$(ssm_get app-key)"
APP_ID="$(ssm_get app-id)"
if [ -z "$APP_KEY" ] || [ "$APP_KEY" = "None" ]; then
  status "waiting for the GitHub App (put the manifest code in $PARAMS/manifest-code)"
  for _ in $(seq 1 720); do
    CODE="$(ssm_get manifest-code)"
    [ -n "$CODE" ] && [ "$CODE" != "None" ] && break
    sleep 10
  done
  [ -n "${CODE:-}" ] && [ "$CODE" != "None" ] || { status "error: no manifest code within two hours"; exit 1; }
  CONVERSION="$(curl -sS -X POST -H 'Accept: application/vnd.github+json' "https://api.github.com/app-manifests/$CODE/conversions")"
  APP_ID="$(printf '%s' "$CONVERSION" | "$VENV/bin/python" -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
  APP_KEY="$(printf '%s' "$CONVERSION" | "$VENV/bin/python" -c 'import json,sys; print(json.load(sys.stdin)["pem"])')"
  unset CONVERSION
  ssm_put app-key SecureString "$APP_KEY"
  ssm_put app-id String "$APP_ID"
  aws ssm delete-parameter --region "$REGION" --name "$PARAMS/manifest-code" || true
  say "GitHub App $APP_ID created"
fi
umask 077
printf '%s\n' "$APP_KEY" > /etc/brick-runner/app-key.pem
unset APP_KEY
chmod 600 /etc/brick-runner/app-key.pem

status "waiting for the GitHub App to be installed on ${BRICK_REPO%%/*}"
INSTALLATION_ID=""
for _ in $(seq 1 720); do
  INSTALLATION_ID="$(BRICK_OWNER="${BRICK_REPO%%/*}" "$VENV/bin/python" - "$APP_ID" /etc/brick-runner/app-key.pem <<'PY' || true
import json, os, sys, urllib.request
from pathlib import Path
from brick_runner.github import AppAuth
jwt = AppAuth(sys.argv[1], Path(sys.argv[2]), "").jwt()
request = urllib.request.Request("https://api.github.com/app/installations",
    headers={"Authorization": f"Bearer {jwt}", "Accept": "application/vnd.github+json", "User-Agent": "brick-runner"})
for installation in json.load(urllib.request.urlopen(request, timeout=30)):
    if installation["account"]["login"].casefold() == os.environ["BRICK_OWNER"].casefold():
        print(installation["id"])
        break
PY
)"
  [ -n "$INSTALLATION_ID" ] && break
  sleep 10
done
[ -n "$INSTALLATION_ID" ] || { status "error: the App was not installed within two hours"; exit 1; }
ssm_put installation-id String "$INSTALLATION_ID"

cat > /etc/brick-runner/env <<EOF
BRICK_RUNNER_APP_ID=$APP_ID
BRICK_RUNNER_INSTALLATION_ID=$INSTALLATION_ID
BRICK_RUNNER_APP_KEY=/run/credentials/brick-runner.service/app-key
AWS_DEFAULT_REGION=$REGION
EOF
chmod 600 /etc/brick-runner/env

# ---------------------------------------------------------------- backup remotes, or restore
status "waiting for the evidence deploy key on $EVIDENCE_REPO (public key in $PARAMS/pub/evidence)"
EVIDENCE_URL="git@github-evidence:$EVIDENCE_REPO.git"
for _ in $(seq 1 720); do
  sudo -u brick-runner git ls-remote "$EVIDENCE_URL" >/dev/null 2>&1 && break
  sleep 10
done
sudo -u brick-runner git ls-remote "$EVIDENCE_URL" >/dev/null 2>&1 || { status "error: the evidence deploy key was not added within two hours"; exit 1; }
as_runner() { sudo -u brick-runner env BRICK_RUNNER_HOME="$HOME_DIR" AWS_DEFAULT_REGION="$REGION" "$VENV/bin/brick-runner" "$@"; }
if sudo -u brick-runner git ls-remote --exit-code "$EVIDENCE_URL" refs/heads/main >/dev/null 2>&1 && [ ! -d "$HOME_DIR/store/.dvc" ]; then
  say "restoring from $EVIDENCE_REPO and s3://$BUCKET/$PREFIX"
  as_runner restore --git "$EVIDENCE_URL"
else
  as_runner remote --url "s3://$BUCKET/$PREFIX" --region "$REGION" --git "$EVIDENCE_URL"
fi

# ---------------------------------------------------------------- the service
cat > /etc/systemd/system/brick-runner.service <<EOF
[Unit]
Description=brick-runner: evaluate pull requests for $BRICK_REPO
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=brick-runner
Group=brick-runner
EnvironmentFile=/etc/brick-runner/env
LoadCredential=app-key:/etc/brick-runner/app-key.pem
Environment=BRICK_RUNNER_HOME=$HOME_DIR
ExecStart=$VENV/bin/brick-runner serve --github $BRICK_REPO --allow $ALLOW --interval 60 --sync-every 600
Restart=on-failure
RestartSec=30
NoNewPrivileges=yes
ProtectSystem=full
StateDirectory=brick-runner
StateDirectoryMode=0700
UMask=0077

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now brick-runner
sleep 5
systemctl is-active --quiet brick-runner && status "running" || status "error: the service did not start"
