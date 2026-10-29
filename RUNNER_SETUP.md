# Runner setup (Firecracker microVM runner on EC2 or any KVM Linux host)

This sets up the runner that this backend already ships (`runner_app.py`, `firecracker_executor.py`, `guest_agent.py`). It is the same for AWS, GCP and other hosts. Commands assume Ubuntu 24.04 on x86_64 and a root shell (`sudo -i`). Run the `./sandbox/...` commands from the folder that contains `sandbox/` and `workspace_manager/`: `agent-runner/` when you use the runner-only zip, or `backend/` if you copied the whole backend.

**Not tested on a real KVM host from here.** The code paths were tested with fake Firecracker/jailer programs. Run the smoke test (step 7) before enabling users.

Order of work: **A** clean the old install, **B** get the backend files onto the host, **0-7** build and start the runner, **C** Cloudflare tunnel and Access, **D** Render settings, **E** end-to-end check.

## A. Remove the old Firecracker, jailer, chroots and toy runner
Look first, delete second. The `rm -rf` lines are permanent: run them only after the listing shows old files you do not need.
```bash
# 1) stop whatever is running
ps aux | grep -Ei 'firecracker|jailer|runner.py|uvicorn' | grep -v grep
systemctl list-units --type=service | grep -Ei 'firecracker|runner|microvm|cloudflared' || true
ss -ltnp | grep -E ':(8443|8765)\b' || true
pkill -f runner.py || true; pkill -x firecracker || true; pkill -x jailer || true
systemctl disable --now agent-runner 2>/dev/null || true

# 2) see what exists (nothing is deleted here)
which -a firecracker jailer; firecracker --version 2>/dev/null; jailer --version 2>/dev/null
ls -ld /srv/jailer /var/lib/agent-microvm /var/lib/firecracker /opt/agent-microvm /tmp/fc-sessions ~/runner ~/vmlinux* ~/rootfs* ~/firecracker* ~/release-* 2>/dev/null
findmnt -rno TARGET | grep -E '/srv/jailer|/var/lib/agent-microvm|/var/lib/firecracker' || echo "no leftover mounts"

# 3) unmount leftover jail mounts, then delete the old install
findmnt -rno TARGET | grep -E '/srv/jailer|/var/lib/agent-microvm|/var/lib/firecracker' | sort -r | xargs -r -n1 umount -l
rm -f /usr/local/bin/firecracker /usr/local/bin/jailer /usr/bin/firecracker /usr/bin/jailer /usr/local/bin/agent-microvm-exec /etc/systemd/system/agent-runner.service
rm -rf /srv/jailer /var/lib/agent-microvm /var/lib/firecracker /opt/agent-microvm /tmp/fc-sessions
rm -rf ~/runner ~/vmlinux* ~/rootfs.ext4 ~/firecracker* ~/release-*
systemctl daemon-reload; hash -r
which firecracker jailer || echo "old binaries are gone"
```

## B. Put the runner files on the host
The host needs only `agent-runner.zip` (the `sandbox/` and `workspace_manager/` folders plus a small requirements file), not the whole backend. Session Manager cannot upload files, so use a short-lived download link:
```bash
# on your laptop (AWS CLI configured): upload and make a 15-minute link
aws s3 cp agent-runner.zip s3://YOUR_BUCKET/agent-runner.zip
aws s3 presign s3://YOUR_BUCKET/agent-runner.zip --expires-in 900

# on the EC2 host
apt-get install -y unzip
cd /root && curl -fSL -o agent-runner.zip 'PASTE_THE_PRESIGNED_URL' && unzip -q -o agent-runner.zip
cd /root/agent-runner          # every "./sandbox/..." command below runs from here
```

## 0. Host requirements
- `/dev/kvm` must exist: `ls -l /dev/kvm`. On EC2 that means a `.metal` instance, or a C8i/M8i/R8i with **Nested virtualization = Enable** (Advanced details at launch). On GCE enable nested virtualization on the VM.
- Dedicated host, no other workloads, no inbound ports (use a Cloudflare Tunnel or a private network).

## 1. Packages
```bash
apt-get update
apt-get install -y python3-venv python3-pip debootstrap e2fsprogs curl ca-certificates tar jq unzip
```

## 2. Firecracker and jailer
```bash
ARCH=$(uname -m)
RELEASES=https://github.com/firecracker-microvm/firecracker/releases
VER=$(basename "$(curl -fsSLI -o /dev/null -w '%{url_effective}' $RELEASES/latest)")
cd /tmp
curl -fSLO $RELEASES/download/$VER/firecracker-$VER-$ARCH.tgz
sha256sum firecracker-$VER-$ARCH.tgz            # compare with the checksum on the release page before continuing
tar -xzf firecracker-$VER-$ARCH.tgz
install -m0755 release-$VER-$ARCH/firecracker-$VER-$ARCH /usr/local/bin/firecracker
install -m0755 release-$VER-$ARCH/jailer-$VER-$ARCH      /usr/local/bin/jailer
firecracker --version && jailer --version
```

## 3. Guest kernel (Firecracker's CI kernel; it has the vsock support the guest agent needs)
```bash
CI_VERSION=${VER%.*}
KEY=$(curl -s "http://spec.ccfc.min.s3.amazonaws.com/?prefix=firecracker-ci/$CI_VERSION/$ARCH/vmlinux-&list-type=2" \
  | grep -oP "(?<=<Key>)(firecracker-ci/$CI_VERSION/$ARCH/vmlinux-[0-9]+\.[0-9]+\.[0-9]{1,3})(?=</Key>)" | sort -V | tail -1)
echo "kernel: $KEY"
mkdir -p /var/lib/agent-microvm
curl -fSL -o /var/lib/agent-microvm/vmlinux "https://s3.amazonaws.com/spec.ccfc.min/$KEY"
```

## 4. Guest root filesystem (about 10 to 20 minutes; needs internet only for the build)
```bash
./sandbox/build_rootfs.sh            # writes /var/lib/agent-microvm/agent-rootfs.ext4 and installs the guest agent into it
# WITH_OFFICE=1 ./sandbox/build_rootfs.sh   adds LibreOffice (large) for Office-to-PDF conversion and previews
```
The VMs have no network, so every tool the agents' code needs must be in this image. Rebuild it when you change the package list.

## 5. Install the runner
```bash
./sandbox/install_runner.sh ./sandbox
python3 -m venv /opt/agent-microvm/venv
/opt/agent-microvm/venv/bin/pip install -r requirements-runner.txt

python3 -c "import secrets; print(secrets.token_urlsafe(48))"      # use the output as SANDBOX_RUNNER_SECRET (same value in the backend)
install -m 0600 /dev/null /etc/agent-runner.env
cat > /etc/agent-runner.env <<'ENV'
SANDBOX_BACKEND=microvm
SANDBOX_MICROVM_EXECUTOR=/usr/local/bin/agent-microvm-exec
SANDBOX_RUNNER_SECRET=PUT_THE_SECRET_HERE
APP_ENV=production
FIRECRACKER_BIN=/usr/local/bin/firecracker
FIRECRACKER_JAILER=/usr/local/bin/jailer
FIRECRACKER_KERNEL=/var/lib/agent-microvm/vmlinux
FIRECRACKER_ROOTFS=/var/lib/agent-microvm/agent-rootfs.ext4
FIRECRACKER_RUN_ROOT=/var/lib/agent-microvm/sessions
FIRECRACKER_USE_JAILER=1
FIRECRACKER_MEM_MIB=512
RUNNER_MAX_PARALLEL=4
SANDBOX_TMP=/var/lib/agent-microvm/tmp
# FIRECRACKER_DEBUG_LOG=1     # only while debugging: writes the guest console to <session dir>/firecracker.log
ENV
mkdir -p /var/lib/agent-microvm/tmp
systemctl daemon-reload && systemctl enable --now agent-runner
systemctl status agent-runner --no-pager
```
The runner listens on `127.0.0.1:8765` only. It runs as root because the jailer needs root; Firecracker then drops to `nobody`.

## 6. Check the host
```bash
set -a; . /etc/agent-runner.env; set +a
/usr/local/bin/agent-microvm-exec --health          # every check should be true: {"ok": true, ...}
curl -s -H "X-Runner-Secret: $SANDBOX_RUNNER_SECRET" http://127.0.0.1:8765/health
# expected: {"ok":true,"backend":"microvm","detail":"microVM executor"}
```

## 7. Smoke test (boots a real microVM)
```bash
set -a; . /etc/agent-runner.env; set +a
/opt/agent-microvm/venv/bin/python /opt/agent-microvm/app/sandbox/runner_smoke_test.py http://127.0.0.1:8765
```
It checks a cold-start run, files coming back, a second run in the same VM, no network inside the guest, the time limit, and cleanup. All lines must say PASS. After part C, run it again from your laptop through the tunnel URL with `SANDBOX_RUNNER_SECRET`, `CF_ACCESS_CLIENT_ID` and `CF_ACCESS_CLIENT_SECRET` set (needs `pip install requests`).

## C. Cloudflare: tunnel, service token, Access (menu names move around; search for the term in the dashboard)
Needs a domain whose DNS is on Cloudflare. Examples use `runner.yourdomain.com`. If the Cloudflare dashboard asks for a plan, the free plan is enough.

1. **Create the tunnel.** Zero Trust dashboard, Networks, Tunnels, Create a tunnel, type Cloudflared, name it `ec2-firecracker-runner`. Copy the token from the install screen (treat it like a password).
2. **Install cloudflared on EC2** and connect it with that token:
```bash
mkdir -p --mode=0755 /usr/share/keyrings
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main' | tee /etc/apt/sources.list.d/cloudflared.list
apt-get update && apt-get install -y cloudflared
cloudflared service install PASTE_TUNNEL_TOKEN
systemctl status cloudflared --no-pager         # active (running); the tunnel shows Healthy in the dashboard
```
   The instance only needs outbound internet (the default security group allows it). No inbound rules.
3. **Create a service token** (Access, Service credentials or Service Auth, Create service token), name `render-backend`. Copy the **Client ID** and **Client Secret** now; the secret is shown once.
4. **Create the Access application before the route**, so the hostname is never open: Access, Applications, Create new application, Self-hosted. Public hostname `runner.yourdomain.com`. Add a policy: Action **Service Auth**, Include **Service Token** = `render-backend`. Turn on **401 response for Service Auth policies**, so a missing token gets a clear 401 and not a login redirect. Save.
5. **Add the route.** Back in the tunnel, Routes, Add route, **Published application**: hostname `runner.yourdomain.com`, service type HTTP, URL `127.0.0.1:8765`. Cloudflare creates the DNS record when the domain is on Cloudflare.

Test from your laptop (use the real values):
```bash
curl -i https://runner.yourdomain.com/health                                   # 401: stopped at Cloudflare
curl -i -H "CF-Access-Client-Id: ID" -H "CF-Access-Client-Secret: SECRET" \
        https://runner.yourdomain.com/health                                   # 404: Cloudflare let it in, the runner hides without its own secret
curl -s -H "CF-Access-Client-Id: ID" -H "CF-Access-Client-Secret: SECRET" -H "X-Runner-Secret: RUNNER_SECRET" \
        https://runner.yourdomain.com/health                                   # {"ok":true,"backend":"microvm",...}
```

## D. Render: backend environment variables (Environment tab, then redeploy)
```text
SANDBOX_BACKEND=remote
SANDBOX_RUNNER_URL=https://runner.yourdomain.com
SANDBOX_RUNNER_SECRET=<exactly the value in /etc/agent-runner.env on EC2>
CF_ACCESS_CLIENT_ID=<service token client id>
CF_ACCESS_CLIENT_SECRET=<service token client secret>
```
`aws` with `AWS_RUNNER_URL` instead of `remote` with `SANDBOX_RUNNER_URL` behaves the same. Do not set any instance ID or AWS credentials: with only a URL the backend never calls the AWS API.

## E. End-to-end check
1. Admin, Code screen (`GET /admin/code`): sandbox shows `usable: true`. If not, its `detail` says why (wrong secret, token rejected, tunnel down).
2. In the app, ask an agent to run a small Python snippet and to make a chart file. The first run after idle is slower (cold VM boot).
3. Runs are cut at about 75 seconds because Cloudflare drops requests after 100 seconds.

## If something fails
| Symptom | What to check |
|---|---|
| `/health` says `executor is unhealthy` | `agent-microvm-exec --health` shows which of firecracker, jailer, kernel, rootfs or kvm is false. |
| `Firecracker exited during startup` | Set `FIRECRACKER_DEBUG_LOG=1`, retry, read `/var/lib/agent-microvm/sessions/<id>/firecracker.log`. To tell a jailer problem from a VM problem, try `FIRECRACKER_USE_JAILER=0` once (less isolation: for diagnosis only). |
| `guest agent did not become ready` | The VM booted without the agent: rebuild the rootfs (step 4), and confirm the kernel supports vsock. |
| `needs the executor to run as root` | The runner service must run as root (see `agent-runner.service`). |
| Runs stop at about 75 seconds through Cloudflare | Expected: Cloudflare drops requests after 100 seconds. See the README section. |
| Logs | `journalctl -u agent-runner -n 100 --no-pager` |

## Maintenance
- Patch the host and update Firecracker/jailer by replacing the two binaries; rebuild the rootfs for package updates.
- Abandoned sessions are destroyed by the hourly reaper timer after 24 hours (`FIRECRACKER_SESSION_TTL_SECONDS`).
- Keep the runner secret and the Cloudflare service token out of the frontend, logs and chat.
