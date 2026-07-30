# MySQL on Oracle Cloud Always Free — cloud-init

`mysql-setup.yaml` bootstraps a fresh Ubuntu VM into a working MySQL
Community Server on first boot: installs the package with the data and
engine files split across two Block Volumes, opens it to remote
connections, creates an `appdb`/`appuser` with a random password, and
locks down the defaults (removes anonymous users and the `test` database).

## Required Block Volumes

Create and attach two Block Volumes to the instance **before** first boot
(paravirtualized attachment, so they show up as plain disks):

| Volume | Mounted at | Holds |
|---|---|---|
| `jayoh-mysql-data-vol01` | `/data/mysql` | table data (`datadir`) |
| `jayoh-mysql-engine-vol01` | `/data/mysql-engine` | InnoDB system tablespace, redo log, binlog |

The script assumes they attach as `/dev/sdb` and `/dev/sdc` in that order.
If `lsblk` shows different device names after boot, edit `DATA_DEV`/
`ENGINE_DEV` at the top of the volume-formatting step before running it, or
SSH in and re-run that step manually.

## Usage

1. In OCI, when creating the compute instance, attach the two Block
   Volumes above, then expand **Advanced Options** and paste the contents
   of `mysql-setup.yaml` into **Cloud-init script**.
2. After the instance boots, open **VCN → Security List** (or the
   instance's Network Security Group) and add an ingress rule for TCP
   3306 from the IPs that should reach the DB. Cloud-init only configures
   the OS firewall (`ufw`) — it cannot touch this OCI network resource.
3. SSH into the instance and read `/root/mysql_app_credentials.txt` for
   the generated database name, user, and password.

## Notes

- Restrict the Security List source CIDR to your application server's IP
  rather than `0.0.0.0/0` where possible.
- The generated credentials file is only ever written on the instance
  itself — nothing is stored in this repo.
- The datadir/innodb config (`99-datadir.cnf`) and AppArmor override are
  written *before* `mysql-server` is installed, so Debian's own package
  init writes the split files straight to the two volumes — no data
  migration step is needed.
