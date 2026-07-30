# MySQL on Oracle Cloud Always Free — cloud-init

`mysql-setup.yaml` bootstraps a fresh Ubuntu VM into a working MySQL
Community Server on first boot: installs the package, opens it to remote
connections, creates an `appdb`/`appuser` with a random password, and
locks down the defaults (removes anonymous users and the `test` database).

## Usage

1. In OCI, when creating the compute instance, expand **Advanced Options**
   and paste the contents of `mysql-setup.yaml` into **Cloud-init script**.
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
