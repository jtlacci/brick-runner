# The runner on AWS

One EC2 instance, one S3 bucket, one IAM role, and SSM Parameter Store for
secrets. No SSH, no open ports, no AWS access keys.

| Resource | Setting |
| --- | --- |
| EC2 | Ubuntu 24.04, `t3.medium`, 30 GB gp3 encrypted, IMDSv2 only, default VPC, public IP for outbound traffic |
| Security group | No inbound rules |
| Access | AWS Systems Manager (Session Manager and Run Command) only |
| IAM role | `trust-policy.json` + `role-policy.json` + `AmazonSSMManagedInstanceCore` |
| S3 | Versioning on, public access blocked, default encryption; the role can read and write `brick-runner/*` but never delete |
| SSM parameters | Under `/brick-runner/` (below) |

`bootstrap.sh` is the instance's user data. It installs the runner at a pinned
commit, then waits for three things, and publishes its progress in
`/brick-runner/status`:

1. **The GitHub App.** Create it from `../github-app-manifest.json` (the manifest flow), and put the one-time code from the redirect URL in `/brick-runner/manifest-code`. The instance exchanges it for the App's key itself; the key never leaves the machine except into `/brick-runner/app-key`.
2. **The App's installation** on the brick repository's owner.
3. **The evidence deploy key.** The instance generates it and publishes the public half in `/brick-runner/pub/evidence`. Add it to the evidence repository with write access.

| Parameter | Type | Written by |
| --- | --- | --- |
| `status` | String | the instance |
| `manifest-code` | String | you, once; the instance deletes it |
| `app-id`, `installation-id` | String | the instance |
| `app-key` | SecureString | the instance |
| `runner-key` | SecureString | the instance (the signing key, for restores) |
| `pub/evidence` | String | the instance |

**Restore:** launch a new instance with the same user data. It reuses the keys
in SSM and, because the evidence repository already has a `main` branch,
restores the store and the ledger from Git and S3.

Read the logs without SSH:

```sh
aws ssm start-session --target <instance-id>
sudo journalctl -u brick-runner -f
sudo cat /var/log/brick-runner-bootstrap.log
```
