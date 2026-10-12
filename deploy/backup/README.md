# Daily Postgres backup to S3 Glacier

`pg-weekly-backup.sh` dumps the control-plane database (accounts, keys,
subscriptions, usage), checks the dump restores, and uploads it to
`s3://contextshrink-weekly-backup/postgres/<year>/` as **S3 Glacier Flexible
Retrieval** (storage class `GLACIER`) with S3-managed encryption. The newest 7
dumps also stay on the Pi in `~/backups/contextshrink/` for quick restores.

It runs **every night at 23:30** Pi time (America/Chicago) from the
`raspberrypi5` user's crontab.

## 1. An upload-only AWS key

Create an IAM user (for example `contextshrink-backup-pi5`) with only this
policy, then create an access key for it. The key can add backups but cannot
read, list or delete them, so a compromised Pi cannot wipe them.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "s3:PutObject",
      "Resource": "arn:aws:s3:::contextshrink-weekly-backup/postgres/*"
    }
  ]
}
```

Recommended on the bucket: Block Public Access on, and versioning on (so even
a same-name upload cannot replace an older backup).

## 2. Put the key on the Pi 5

On the Pi itself (not in chat, not in the repo):

```bash
mkdir -p ~/.config/contextshrink-backup
nano ~/.config/contextshrink-backup/backup.env
chmod 600 ~/.config/contextshrink-backup/backup.env
```

with:

```
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_DEFAULT_REGION=us-east-1   # the bucket's region
```

## 3. Test, then schedule

```bash
~/horizon/deploy/backup/pg-weekly-backup.sh --dry-run   # dump + check, no upload
~/horizon/deploy/backup/pg-weekly-backup.sh             # one real upload
```

Crontab line (`crontab -e`):

```
30 23 * * * /home/raspberrypi5/horizon/deploy/backup/pg-weekly-backup.sh >> /home/raspberrypi5/backups/contextshrink/backup.log 2>&1
```

`~/backups/contextshrink/last_success` holds the time of the last good upload;
`backup.log` has every run.

## Restoring

Glacier Flexible Retrieval objects must be restored before download. Standard
retrieval takes 3-5 hours (Expedited: minutes, at extra cost). With a key that
can read the bucket (not the Pi's upload-only key):

```bash
aws s3api restore-object --bucket contextshrink-weekly-backup \
  --key postgres/2026/contextshrink-<stamp>.dump \
  --restore-request '{"Days":3,"GlacierJobParameters":{"Tier":"Standard"}}'
# when `aws s3api head-object ...` shows ongoing-request="false":
aws s3 cp s3://contextshrink-weekly-backup/postgres/2026/contextshrink-<stamp>.dump .
```

Restore into a running stack (this replaces the current data):

```bash
docker exec -i contextshrink-postgres pg_restore -U contextshrink -d contextshrink \
  --clean --if-exists --no-owner < contextshrink-<stamp>.dump
docker compose -f docker-compose.controlplane.yml restart api
```

A local copy in `~/backups/contextshrink/` restores the same way, without the
Glacier wait.

## Costs

Glacier Flexible Retrieval bills each object for at least 90 days, and a
retrieval costs a little per GB. Daily dumps of this database are small (well
under 100 MB), so storage stays at cents per month.

## Why daily

The Pi 5 keeps the database on an SD card, the storage that wears out under
constant writes. A weekly backup could lose a week of signups and usage; daily
caps it at a day. The script name and bucket still say "weekly" from the
first version.

## Disk upkeep on the Pi 5

Every proxy rebuild leaves Docker build cache behind (71 GB had piled up by
2026-10-11). A second crontab line clears cache older than three days each
Monday, keeping the latest build's layers so the next rebuild stays fast:

```
0 4 * * 1 docker builder prune -f --filter until=72h > /dev/null 2>&1
```


## Retention (the Privacy Policy depends on this)

The Pi's upload key cannot delete objects, so expiry is an S3 lifecycle rule
on the bucket: **contextshrink-weekly-backup → Management → Create lifecycle
rule**, prefix `postgres/`, action "Expire current versions of objects" after
**90 days**. The Privacy Policy promises backups are kept for up to 90 days;
change both together. (Glacier Flexible Retrieval bills a 90-day minimum per
object anyway, so a shorter rule saves nothing.)
