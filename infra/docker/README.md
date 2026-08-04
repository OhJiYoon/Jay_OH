# MySQL via Docker Compose (test use)

Lightweight MySQL 8 container for local/dev testing on the OCI instance
(or anywhere Docker runs). Data persists in `./data` via a bind mount, so
recreating the container keeps the database.

## Usage

```bash
cd infra/docker
cp .env.example .env
# edit .env: set real passwords
docker compose up -d
```

Connect:
```bash
mysql -h 127.0.0.1 -P 3306 -u appuser -p appdb
```

Stop (keeps data):
```bash
docker compose down
```

Stop and wipe data:
```bash
docker compose down
rm -rf data
```

## Notes

- `.env` and `data/` are gitignored — real credentials and the database
  files never get committed.
- If running on the OCI instance, install Docker first
  (`sudo dnf install -y docker` + `sudo systemctl enable --now docker` on
  Oracle Linux), open TCP 3306 in the OCI Security List same as before,
  and make sure the instance user is in the `docker` group or use `sudo`.
