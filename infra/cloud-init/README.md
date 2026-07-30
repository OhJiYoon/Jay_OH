# MySQL on Oracle Cloud Always Free — cloud-init

`mysql-setup.yaml` bootstraps a fresh **Oracle Linux 8/9** VM into a working
MySQL Community Server on first boot: installs the package with the data
and engine files split across two Block Volumes, opens it to remote
connections, creates an `appdb`/`appuser` with a random password, and
locks down the defaults (removes anonymous users and the `test` database).

SSH into these instances as `opc` (Oracle Linux cloud image default user),
not `ubuntu`.

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
   the OS firewall (`firewalld`) — it cannot touch this OCI network
   resource.
3. SSH into the instance and read `/root/mysql_app_credentials.txt` for
   the generated root password and app database name/user/password.

## Notes

- Restrict the Security List source CIDR to your application server's IP
  rather than `0.0.0.0/0` where possible.
- The generated credentials file is only ever written on the instance
  itself — nothing is stored in this repo.
- The datadir/innodb config (`99-datadir.cnf`) is written *before*
  `mysqld` first starts, so its own init step (triggered by
  `systemctl enable --now mysqld`) writes the split files straight to the
  two volumes — no data migration step is needed.
- Unlike Debian/Ubuntu (unix-socket root auth, no root password), Oracle
  Linux's MySQL logs a one-time temporary root password to
  `/var/log/mysqld.log` on first start and requires an immediate
  `ALTER USER` before any other statement — the script does this and
  stores the new root password in the credentials file.
- SELinux (not AppArmor) is what needs the `/data/mysql*` paths labeled
  `mysqld_db_t` via `semanage fcontext` + `restorecon`; without this,
  `mysqld` fails to start with a permissions error even though the Unix
  file ownership is correct.
